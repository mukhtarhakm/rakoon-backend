import hashlib
import hmac
import logging
import os
import re
import secrets
import uuid
from typing import Optional, Tuple

from fastapi import HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool

from app.database import supabase

logger = logging.getLogger("rakoon_backend.storage_upload")

SUPABASE_BUCKET = "product-photos"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
READ_CHUNK_BYTES = 64 * 1024

_IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
)


async def read_upload(file: UploadFile, max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
    """Baca berkas bertahap; berhenti dan 413 begitu melewati batas, 400 bila kosong."""
    chunks = []
    total = 0
    while True:
        chunk = await file.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Ukuran file terlalu besar. Maksimum {max_bytes // (1024 * 1024)}MB.",
            )
        chunks.append(chunk)
    if total == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Berkas yang diunggah kosong.")
    return b"".join(chunks)


def detect_content_type(contents: bytes, allow_pdf: bool = False) -> Tuple[str, str]:
    """Tipe ditentukan hanya dari magic bytes; nama berkas dan Content-Type klien diabaikan."""
    for signature, content_type, ext in _IMAGE_SIGNATURES:
        if contents.startswith(signature):
            return content_type, ext
    if contents.startswith(b"RIFF") and contents[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if allow_pdf and contents.startswith(b"%PDF"):
        return "application/pdf", ".pdf"
    kinds = "PDF, JPEG, PNG, atau WebP" if allow_pdf else "JPEG, PNG, atau WebP"
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Format file tidak valid. Gunakan {kinds}.",
    )


def store_public(contents: bytes, content_type: str, ext: str, prefix: str = "") -> str:
    """Unggah ke Supabase Storage dengan nama acak dan kembalikan URL publik. Tanpa fallback."""
    filename = f"{prefix}{uuid.uuid4().hex}{ext}"
    try:
        bucket = supabase.storage.from_(SUPABASE_BUCKET)
        bucket.upload(filename, contents, {"content-type": content_type, "upsert": "true"})
        public_url = bucket.get_public_url(filename)
    except Exception as exc:
        logger.error(f"Gagal mengunggah ke Supabase Storage: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Penyimpanan berkas sedang tidak tersedia. Coba lagi nanti.",
        )
    if not public_url:
        logger.error("Supabase Storage tidak mengembalikan URL publik.")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Penyimpanan berkas tidak mengembalikan URL.",
        )
    return public_url.rstrip("?")


async def upload_image(file: UploadFile, prefix: str = "") -> str:
    contents = await read_upload(file)
    content_type, ext = detect_content_type(contents)
    # Klien supabase sinkron: jalankan di thread terpisah agar event loop tidak terblokir.
    return await run_in_threadpool(store_public, contents, content_type, ext, prefix)


# ---- Bukti klaim (dokumen legalitas + foto toko): bucket PRIVAT, hanya PDF/JPEG ----
CLAIM_BUCKET = "claim-document"
CLAIM_SIGNED_URL_TTL_SECONDS = 300
CLAIM_PATH_RE = re.compile(r"^(documents|photos)/([0-9a-f]{32})([0-9a-f]{16})\.(pdf|jpg|png)$")
_CLAIM_EXTENSIONS = {"documents": {"pdf", "jpg"}, "photos": {"jpg", "png"}}


def _claim_mac_key() -> Optional[bytes]:
    secret = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    if not secret:
        return None
    return hashlib.sha256(b"claim-evidence-path:" + secret.encode("utf-8")).digest()


def _claim_mac(key: bytes, kind: str, user_id: str, token: str) -> str:
    return hmac.new(key, f"{kind}:{user_id}:{token}".encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def detect_claim_evidence_type(contents: bytes, kind: str) -> Tuple[str, str]:
    """Dokumen: PDF atau JPEG. Foto toko: JPEG atau PNG. Dicek dari magic bytes."""
    if contents.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if kind == "documents" and contents.startswith(b"%PDF"):
        return "application/pdf", ".pdf"
    if kind == "photos" and contents.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    allowed = "PDF atau JPEG" if kind == "documents" else "JPEG atau PNG"
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Format file tidak valid. Gunakan {allowed}.",
    )


def new_claim_path(kind: str, ext: str, user_id: str) -> str:
    """Path acak `{kind}/{32 hex}{16 hex MAC}{ext}`; MAC mengikat path ke user_id tanpa memuatnya."""
    key = _claim_mac_key()
    if key is None:
        raise RuntimeError("SUPABASE_SERVICE_KEY kosong")
    token = secrets.token_hex(16)
    return f"{kind}/{token}{_claim_mac(key, kind, str(user_id), token)}{ext}"


def store_private(contents: bytes, content_type: str, ext: str, kind: str, user_id: str) -> str:
    """
    Unggah bukti klaim ke bucket privat dan kembalikan path objek (bukan URL).
    Path acak tanpa user_id; kepemilikan dibuktikan lewat MAC (claim_path_belongs_to), tanpa tabel baru.
    Gagal -> 503; tidak ada fallback.
    """
    key = _claim_mac_key()
    if key is None:
        logger.error("SUPABASE_SERVICE_KEY kosong: unggahan bukti klaim ditolak.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Penyimpanan berkas sedang tidak tersedia. Coba lagi nanti.",
        )
    path = new_claim_path(kind, ext, user_id)
    try:
        supabase.storage.from_(CLAIM_BUCKET).upload(path, contents, {"content-type": content_type, "upsert": "false"})
    except Exception as exc:
        logger.error(f"Gagal mengunggah bukti klaim: {type(exc).__name__}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Penyimpanan berkas sedang tidak tersedia. Coba lagi nanti.",
        )
    return path


def claim_path_belongs_to(path: object, user_id: str, kind: str) -> bool:
    """True bila path berbentuk benar, berjenis `kind`, dan dibuat untuk user_id ini."""
    key = _claim_mac_key()
    match = CLAIM_PATH_RE.match(path) if isinstance(path, str) else None
    if key is None or not match or match.group(1) != kind or match.group(4) not in _CLAIM_EXTENSIONS.get(kind, ()):
        return False
    return hmac.compare_digest(match.group(3), _claim_mac(key, kind, str(user_id), match.group(2)))


def create_claim_signed_url(path: str, expires_in: int = CLAIM_SIGNED_URL_TTL_SECONDS) -> str:
    """Signed URL berumur pendek untuk satu objek di bucket privat. Gagal -> 503/502."""
    try:
        data = supabase.storage.from_(CLAIM_BUCKET).create_signed_url(path, expires_in)
    except Exception as exc:
        logger.error(f"Gagal membuat signed URL bukti klaim: {type(exc).__name__}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Penyimpanan berkas sedang tidak tersedia. Coba lagi nanti.",
        )
    url = (data or {}).get("signedURL") or (data or {}).get("signedUrl")
    if not url:
        logger.error("Supabase Storage tidak mengembalikan signed URL.")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Penyimpanan berkas tidak mengembalikan URL.")
    return url
