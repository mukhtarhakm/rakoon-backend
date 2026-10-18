import os
import base64
import json
import re
import time
import logging
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Union
from uuid import UUID

import httpx
from fastapi import APIRouter, File, UploadFile, HTTPException, Query, status, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.models.schemas import (
    ScanResponse,
    ScanResultItem,
    ConfirmRequest,
    ConfirmResponse,
    MAX_PAGE_SIZE,
    ProductCategory,
    VerificationStatus,
    RecentScanItem,
    ScanSessionProductItem,
    ScanSessionDetailResponse,
)
from app.models.db_models import Product, PriceEntry, Store, ScanSession
from app.database import get_db
from app.dependencies import get_current_user
from app.services.errors import safe_error
from app.services.storage_upload import read_upload, detect_content_type
from app.routers.recommendation import canonical_unit, normalize_unit_and_dimension
from app.services.vision_service import process_shelf_image


logger = logging.getLogger("rakoon_backend.scan")

router = APIRouter()


class SimpleRateLimiter:
    """
    Lightweight in-memory sliding window rate limiter per key (user_id).
    Membatasi jumlah panggilan per rentang waktu untuk mencegah bot/abuse.
    Batas: state per proses (hilang saat restart, tidak dibagi antar worker).
    """
    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests = defaultdict(list)

    def check(self, client_key: str):
        now = time.time()
        # Bersihkan timestamp di luar jendela waktu
        self.requests[client_key] = [ts for ts in self.requests[client_key] if now - ts < self.window_seconds]
        if len(self.requests[client_key]) >= self.max_requests:
            retry_after = int(self.window_seconds - (now - self.requests[client_key][0]))
            logger.warning("Rate limit scan terlampaui")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Terlalu banyak permintaan scan foto. Silakan coba lagi dalam {max(1, retry_after)} detik.",
                headers={"Retry-After": str(max(1, retry_after))}
            )
        self.requests[client_key].append(now)


scan_rate_limiter = SimpleRateLimiter(max_requests=10, window_seconds=60)
MAX_SCAN_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB, khusus scan; unggahan lain tetap 5 MB

# Moderasi harga hasil konfirmasi (P5-02): harga di luar [0.3x, 3x] median harga terverifikasi produk
# menjadi 'pending'. Median baru dipakai bila ada cukup sampel terverifikasi.
PRICE_BAND_LOW_FACTOR = 0.3
PRICE_BAND_HIGH_FACTOR = 3.0
PRICE_BAND_MIN_SAMPLES = 3
# Jumlah entri harga yang boleh dikonfirmasi satu pengguna per hari (UTC).
DAILY_CONFIRM_ITEM_QUOTA = 200
# Entri (produk, toko, harga) yang sama dari pengguna yang sama dalam jendela ini dianggap kiriman ulang dan dilewati.
DUPLICATE_ENTRY_WINDOW = timedelta(minutes=10)


@router.post("/", response_model=ScanResponse, status_code=status.HTTP_200_OK)
async def scan_shelf_photo(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user)
):
    """
    Menerima file foto rak (multipart/form-data) dan memprosesnya melalui
    AI Vision Pipeline menggunakan model gpt-5.6-luna (OpenAI). Wajib login.
    Dilindungi dengan rate limiter per pengguna (maks 10 scan/menit; IP dan X-Forwarded-For
    tidak dipakai karena bisa dipalsukan) dan validasi ukuran file (maks 10 MB).
    """
    # 0. Rate limiting per user_id (dari token terverifikasi)
    scan_rate_limiter.check(user_id)

    # 1. Validasi file upload
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nama file tidak valid."
        )
        
    ext = os.path.splitext(file.filename.lower())[1]
    if ext not in ('.jpg', '.jpeg', '.png', '.webp'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File yang diunggah bukan gambar yang didukung (gunakan JPG, JPEG, PNG, atau WEBP)."
        )

    # 2. Baca bertahap (413 bila > MAX_SCAN_IMAGE_BYTES, 400 bila kosong); tipe ditentukan dari magic bytes
    contents = await read_upload(file, MAX_SCAN_IMAGE_BYTES)
    content_type, _ = detect_content_type(contents)

    # 4. Memproses gambar melalui vision service (gpt-5.6-luna)
    # Log hanya metrik tak-pribadi: tanpa user_id dan tanpa nama berkas unggahan.
    started = time.monotonic()
    try:
        result = await process_shelf_image(contents, content_type)
    except HTTPException as e:
        logger.warning("scan gagal: status=%d bytes=%d durasi_ms=%d", e.status_code, len(contents), (time.monotonic() - started) * 1000)
        raise
    except Exception as e:
        logger.error("scan gagal: error=%s bytes=%d durasi_ms=%d", type(e).__name__, len(contents), (time.monotonic() - started) * 1000)
        raise
    logger.info("scan selesai: status=200 items=%d bytes=%d durasi_ms=%d", len(result.detected), len(contents), (time.monotonic() - started) * 1000)
    return result


def _find_product_by_name(db: Session, clean_name: str) -> Optional[Product]:
    """Cocokkan nama produk persis tanpa beda huruf besar/kecil; % dan _ pada input bersifat literal."""
    return db.query(Product).filter(func.lower(Product.nama) == clean_name.lower()).first()


_SIZE_SUFFIX_RE = re.compile(r"\s*\(\s*\d+(?:[.,]\d+)?\s*[A-Za-z]+\s*\)\s*$")


def _size_key(ukuran, satuan):
    """(satuan dasar, ukuran dalam satuan dasar) untuk perbandingan ukuran; None bila ukuran/satuan kosong atau satuan tak dikenal."""
    if ukuran is None or ukuran <= 0 or not satuan:
        return None
    normalized = normalize_unit_and_dimension(satuan, ukuran)
    if not normalized:
        return None
    _dimension, base_unit, value = normalized
    return base_unit, round(value, 3)


def _variant_name(clean_name: str, size_key) -> str:
    """'{nama} ({ukuran}{satuan})' dalam satuan dasar, mis. 'Ultra Milk Cokelat (200ml)'; sufiks ukuran lama pada nama dibuang dulu."""
    base_unit, value = size_key
    number = format(value, "f").rstrip("0").rstrip(".")
    return f"{_SIZE_SUFFIX_RE.sub('', clean_name)} ({number}{base_unit})"


def _resolve_product(db: Session, clean_name: str, item):
    """Produk untuk item konfirmasi dan True bila dibuat baru. Nama yang sama tetapi ukuran berbeda masuk ke produk varian."""
    product = None
    # Cek di session's new objects terlebih dahulu untuk menghindari duplikasi dalam batch yang sama
    for obj in db.new:
        if isinstance(obj, Product) and obj.nama.lower() == clean_name.lower():
            product = obj
            break
    if not product:
        product = _find_product_by_name(db, clean_name)

    kategori = item.kategori.value if isinstance(item.kategori, ProductCategory) else item.kategori
    if product:
        item_size, product_size = _size_key(item.ukuran, item.satuan), _size_key(product.ukuran, product.satuan)
        if item_size and product_size and item_size != product_size:
            variant_name = _variant_name(clean_name, item_size)
            product = _find_product_by_name(db, variant_name)
            if not product:
                product = Product(nama=variant_name, ukuran=item.ukuran, satuan=canonical_unit(item.satuan), kategori=kategori)
                db.add(product)
                db.flush()  # IntegrityError bila varian baru saja dibuat pihak lain; confirm_scan_results mengulang dan memakainya
                return product, True
        # Update category only if the existing category is legacy "General"
        if product.kategori.strip().lower() == "general":
            product.kategori = kategori
        return product, False

    product = Product(nama=clean_name, ukuran=item.ukuran, satuan=canonical_unit(item.satuan), kategori=kategori)
    db.add(product)
    db.flush()  # Flush untuk mendapatkan generated ID dari database
    return product, True


def _price_status(db: Session, product_id, harga: int) -> VerificationStatus:
    """VERIFIED bila harga dalam pita wajar median harga terverifikasi produk, selain itu PENDING."""
    samples = [
        h for (h,) in db.query(PriceEntry.harga)
        .filter(PriceEntry.product_id == product_id, PriceEntry.status_verifikasi == VerificationStatus.VERIFIED.value)
        .all()
    ]
    # shortcut: produk dengan < PRICE_BAND_MIN_SAMPLES data terverifikasi tidak dimoderasi; upgrade bila data tipis jadi masalah.
    if len(samples) < PRICE_BAND_MIN_SAMPLES:
        return VerificationStatus.VERIFIED
    median = statistics.median(samples)
    if median * PRICE_BAND_LOW_FACTOR <= harga <= median * PRICE_BAND_HIGH_FACTOR:
        return VerificationStatus.VERIFIED
    return VerificationStatus.PENDING


def _quota_exceeded() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"Kuota konfirmasi harian tercapai (maksimum {DAILY_CONFIRM_ITEM_QUOTA} entri harga per hari). Coba lagi besok.",
    )


def _save_confirmation(db: Session, user_id: str, store_id: str, items, used_today: int = 0) -> ConfirmResponse:
    products_created = 0
    items_pending = 0
    items_skipped = 0

    now_utc = datetime.now(timezone.utc)
    window_start = now_utc - DUPLICATE_ENTRY_WINDOW
    to_save = []  # (product_id, harga, status)
    seen = set()

    for item in items:
        product, created = _resolve_product(db, item.nama_produk.strip(), item)
        product_id = product.id
        if created:
            products_created += 1

        # Entri kembar: sama dalam request ini, atau sudah disimpan pengguna yang sama untuk toko ini dalam 10 menit terakhir.
        key = (product_id, item.harga)
        if key in seen or (not created and db.query(PriceEntry.id).filter(
            PriceEntry.product_id == product_id,
            PriceEntry.store_id == store_id,
            PriceEntry.harga == item.harga,
            PriceEntry.sumber_user_id == user_id,
            PriceEntry.timestamp >= window_start,
        ).first()):
            items_skipped += 1
            continue
        seen.add(key)

        entry_status = _price_status(db, product_id, item.harga)
        if entry_status == VerificationStatus.PENDING:
            items_pending += 1
        to_save.append((product_id, item.harga, entry_status))

    # Kuota dihitung dari entri yang benar-benar disimpan; melewati batas menolak seluruh permintaan (rollback oleh pemanggil).
    if used_today + len(to_save) > DAILY_CONFIRM_ITEM_QUOTA:
        raise _quota_exceeded()

    # Satu scan_session untuk batch ini; tidak dibuat bila semua item dilewati (tidak ada sesi kosong).
    scan_session = None
    if to_save:
        scan_session = ScanSession(user_id=user_id, store_id=store_id, created_at=now_utc)
        db.add(scan_session)
        db.flush() # Flush untuk mendapatkan generated ID scan_session
        for product_id, harga, entry_status in to_save:
            db.add(PriceEntry(
                product_id=product_id,
                store_id=store_id,
                harga=harga,
                sumber_user_id=user_id,
                status_verifikasi=entry_status,
                scan_session_id=scan_session.id,
                timestamp=now_utc
            ))

    # Commit seluruh perubahan sekaligus secara atomic
    db.commit()

    message = f"Berhasil menyimpan {len(to_save)} entri harga. Membuat {products_created} produk baru."
    if items_pending:
        message += f" {items_pending} entri menunggu verifikasi karena harga di luar kisaran wajar."
    if items_skipped:
        message += f" {items_skipped} entri kembar dilewati."
    return ConfirmResponse(
        items_saved=len(to_save),
        products_created=products_created,
        items_pending_review=items_pending,
        message=message,
        scan_session_id=str(scan_session.id) if scan_session else None
    )


@router.post("/confirm", response_model=ConfirmResponse, status_code=status.HTTP_201_CREATED)
def confirm_scan_results(
    request_data: ConfirmRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user)
):
    """
    Menyimpan hasil scan produk ke database setelah dikonfirmasi atau dikoreksi oleh user di frontend.
    Toko harus ada (404 bila tidak). Jika produk belum terdaftar di tabel 'products' (nama sama persis,
    tanpa beda huruf besar/kecil), produk baru dibuat. Bila nama cocok tetapi ukuran (setelah normalisasi satuan)
    berbeda dari produk yang ada, harga masuk ke produk varian "{nama} ({ukuran}{satuan})". Balapan nama produk (IntegrityError) diulang sekali
    sehingga produk yang baru dibuat pihak lain dipakai, bukan 500.
    Setiap entri harga disimpan ke tabel 'price_entries' yang diasosiasikan dengan satu 'scan_sessions'.
    Entri kembar (produk, toko, harga sama) dalam satu request, atau dari pengguna yang sama dalam 10 menit terakhir,
    hanya disimpan sekali dan tidak dihitung ke kuota harian.
    """
    try:
        store_id = str(UUID(str(request_data.store_id)))
    except ValueError:
        store_id = None

    try:
        store_exists = store_id is not None and db.query(Store.id).filter(Store.id == store_id).first() is not None
    except Exception as e:
        raise safe_error(
            logger,
            e,
            "Terjadi kesalahan saat menyimpan konfirmasi scan.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            log_prefix="Error checking store for scan confirmation",
        )
    if not store_exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Toko tidak ditemukan.")

    # Kuota harian per pengguna (UTC): seluruh permintaan ditolak bila melewati batas, tidak disimpan sebagian.
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        used_today = (
            db.query(func.count(PriceEntry.id))
            .filter(PriceEntry.sumber_user_id == user_id, PriceEntry.timestamp >= day_start)
            .scalar()
        ) or 0
    except Exception as e:
        raise safe_error(
            logger,
            e,
            "Terjadi kesalahan saat menyimpan konfirmasi scan.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            log_prefix="Error checking daily confirm quota",
        )

    for attempt in (1, 2):
        try:
            return _save_confirmation(db, user_id, store_id, request_data.items, used_today)
        except IntegrityError as e:
            db.rollback()
            if attempt == 2:
                raise safe_error(
                    logger,
                    e,
                    "Terjadi kesalahan saat menyimpan konfirmasi scan.",
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    log_prefix="Integrity error during scan confirmation",
                )
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            raise safe_error(
                logger,
                e,
                "Terjadi kesalahan saat menyimpan konfirmasi scan.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                log_prefix="Error during scan confirmation",
            )


@router.get("/recent", response_model=List[RecentScanItem], status_code=status.HTTP_200_OK)
def get_recent_scans(
    limit: int = Query(10, ge=1, le=MAX_PAGE_SIZE, description="Maksimum jumlah scan terbaru yang diambil (maks 100)"),
    offset: int = Query(0, ge=0, description="Lewati sejumlah scan terbaru"),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user)
):
    """
    Mengambil riwayat scan terbaru milik user yang terotentikasi (user_id == user_id) berbasis scan_sessions.
    Diurutkan berdasarkan timestamp DESC (terbaru lebih dulu).
    """
    rows = (
        db.query(
            ScanSession,
            Store,
            func.count(PriceEntry.id).label("product_count")
        )
        .outerjoin(Store, ScanSession.store_id == Store.id)
        .outerjoin(PriceEntry, PriceEntry.scan_session_id == ScanSession.id)
        .filter(ScanSession.user_id == user_id)
        .group_by(ScanSession.id, Store.id)
        .order_by(ScanSession.created_at.desc(), ScanSession.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    results = []
    for session, store, product_count in rows:
        store_name = store.nama if store else f"Toko {str(session.store_id)[:8]}"
        ts = session.created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        results.append(
            RecentScanItem(
                id=str(session.id),
                store_id=str(session.store_id),
                store_name=store_name,
                timestamp=ts,
                product_count=product_count or 0,
                store_address=store.alamat if store else None,
            )
        )
    return results


@router.get("/session/{session_id}", response_model=ScanSessionDetailResponse, status_code=status.HTTP_200_OK)
def get_scan_session_detail(
    session_id: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user)
):
    """
    Mengambil detail scan session tertentu milik user yang terotentikasi.
    Memverifikasi kepemilikan session (scan_session.user_id == user_id).
    Jika session tidak ditemukan atau bukan milik user, mengembalikan 404.
    """
    session = (
        db.query(ScanSession)
        .filter(ScanSession.id == session_id, ScanSession.user_id == user_id)
        .first()
    )
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sesi scan tidak ditemukan."
        )

    store = db.query(Store).filter(Store.id == session.store_id).first()
    store_name = store.nama if store else f"Toko {str(session.store_id)[:8]}"

    # Query price entries with product details
    price_entries = (
        db.query(PriceEntry, Product)
        .join(Product, PriceEntry.product_id == Product.id)
        .filter(PriceEntry.scan_session_id == session.id)
        .all()
    )

    items = []
    for pe, product in price_entries:
        items.append(
            ScanSessionProductItem(
                product_id=str(product.id),
                nama_produk=product.nama,
                kategori=product.kategori,
                ukuran=product.ukuran,
                satuan=product.satuan,
                harga=pe.harga,
            )
        )

    ts = session.created_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    return ScanSessionDetailResponse(
        id=str(session.id),
        store_id=str(session.store_id),
        store_name=store_name,
        timestamp=ts,
        product_count=len(items),
        items=items,
        store_address=store.alamat if store else None,
    )




