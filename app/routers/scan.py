import os
import base64
import json
import re
import time
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional, List, Union
from uuid import UUID

import httpx
from fastapi import APIRouter, File, UploadFile, HTTPException, Query, status, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.models.schemas import (
    ScanResponse,
    ScanResultItem,
    ConfirmRequest,
    ConfirmResponse,
    ProductCategory,
    VerificationStatus,
    RecentScanItem,
    ScanSessionProductItem,
    ScanSessionDetailResponse,
)
from app.models.db_models import Product, PriceEntry, Store, ScanSession
from app.database import get_db
from app.dependencies import get_current_user
from app.services.vision_service import process_shelf_image


logger = logging.getLogger("rakoon_backend.scan")

router = APIRouter()


def normalize_and_validate_category(val) -> str:
    cleaned = clean_str(val)
    if not cleaned:
        return ProductCategory.LAINNYA.value
    
    # 1. Check direct matches against ProductCategory values (case-insensitive)
    for member in ProductCategory:
        if member.value.lower() == cleaned.lower():
            return member.value
            
    # 2. Check matches against ProductCategory names (case-insensitive)
    for member in ProductCategory:
        if member.name.lower() == cleaned.replace(" ", "_").replace("&", "").replace("__", "_").lower():
            return member.value
            
    return ProductCategory.LAINNYA.value


def clean_numeric(val) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        val_str = str(val).strip()
        val_str = val_str.replace("Rp", "").replace("rp", "").replace(" ", "")
        return float(val_str)
    except ValueError:
        return None

def clean_str(val) -> Optional[str]:
    if val is None:
        return None
    val_str = str(val).strip()
    if not val_str or val_str.lower() in ("null", "none"):
        return None
    return val_str

def extract_and_parse_json(text: str) -> dict:
    """
    Ekstrak JSON dari teks mentah yang dihasilkan oleh LLM.
    Mendukung format JSON bersih, JSON di dalam block markdown (```json ... ```),
    serta membersihkan tag reasoning/thinking (<think>...</think>) jika ada.
    """
    cleaned = text.strip()
    cleaned = re.sub(r'<think>.*?</think>', '', cleaned, flags=re.DOTALL).strip()
    
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
        
    markdown_match = re.search(r'```(?:json)?\s*(.*?)\s*```', cleaned, re.DOTALL)
    if markdown_match:
        try:
            return json.loads(markdown_match.group(1).strip())
        except json.JSONDecodeError:
            pass
            
    first_brace = cleaned.find('{')
    last_brace = cleaned.rfind('}')
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(cleaned[first_brace:last_brace + 1])
        except json.JSONDecodeError:
            pass
            
    first_bracket = cleaned.find('[')
    last_bracket = cleaned.rfind(']')
    if first_bracket != -1 and last_bracket != -1 and last_bracket > first_bracket:
        try:
            return json.loads(cleaned[first_bracket:last_bracket + 1])
        except json.JSONDecodeError:
            pass
            
    raise json.JSONDecodeError("Gagal mengekstrak JSON dari respon AI.", cleaned, 0)


class SimpleRateLimiter:
    """
    Lightweight in-memory sliding window rate limiter per client IP.
    Membatasi jumlah panggilan per rentang waktu untuk mencegah bot/abuse.
    """
    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests = defaultdict(list)

    def check(self, client_ip: str):
        now = time.time()
        # Bersihkan timestamp di luar jendela waktu
        self.requests[client_ip] = [ts for ts in self.requests[client_ip] if now - ts < self.window_seconds]
        if len(self.requests[client_ip]) >= self.max_requests:
            retry_after = int(self.window_seconds - (now - self.requests[client_ip][0]))
            logger.warning(f"Rate limit exceeded for client IP: {client_ip}")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Terlalu banyak permintaan scan foto. Silakan coba lagi dalam {max(1, retry_after)} detik.",
                headers={"Retry-After": str(max(1, retry_after))}
            )
        self.requests[client_ip].append(now)


scan_rate_limiter = SimpleRateLimiter(max_requests=10, window_seconds=60)
MAX_SCAN_IMAGE_SIZE = 5 * 1024 * 1024  # 5 MB


@router.post("/", response_model=ScanResponse, status_code=status.HTTP_200_OK)
async def scan_shelf_photo(
    request: Request,
    file: UploadFile = File(...)
):
    """
    Menerima file foto rak (multipart/form-data) dan memprosesnya melalui
    AI Vision Pipeline menggunakan model gpt-5.6-luna (OpenAI).
    Dilindungi dengan rate limiter (maks 10 scan/menit per IP) dan validasi ukuran file (maks 5 MB).
    """
    # 0. Rate limiting check per client IP
    client_ip = request.client.host if request.client else "unknown"
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip()
    scan_rate_limiter.check(client_ip)

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

    # 2. Membaca konten file foto dengan batasan ukuran 5 MB
    try:
        contents = await file.read()
        if not contents:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File foto yang diunggah kosong."
            )
        if len(contents) > MAX_SCAN_IMAGE_SIZE:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Ukuran file foto terlalu besar (maksimum 5 MB)."
            )
        logger.info(f"Received file '{file.filename}' ({len(contents)} bytes) from IP {client_ip} for scanning.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reading file content: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Gagal membaca file foto: {str(e)}"
        )

    # 3. Menentukan MIME type
    content_type = file.content_type
    if not content_type or not content_type.startswith("image/"):
        if ext in ('.jpg', '.jpeg'):
            content_type = "image/jpeg"
        elif ext == '.png':
            content_type = "image/png"
        elif ext == '.webp':
            content_type = "image/webp"
        else:
            content_type = "image/jpeg"

    # 4. Memproses gambar melalui vision service (gpt-5.6-luna)
    return await process_shelf_image(contents, content_type)


@router.post("/confirm", response_model=ConfirmResponse, status_code=status.HTTP_201_CREATED)
def confirm_scan_results(
    request_data: ConfirmRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user)
):
    """
    Menyimpan hasil scan produk ke database setelah dikonfirmasi atau dikoreksi oleh user di frontend.
    Jika produk belum terdaftar di tabel 'products' (berdasarkan nama case-insensitive), produk baru akan dibuat.
    Setiap entri harga akan disimpan ke tabel 'price_entries' yang diasosiasikan dengan satu 'scan_sessions'.
    """
    items_saved = 0
    products_created = 0
    
    try:
        now_utc = datetime.now(timezone.utc)
        # 1. Buat satu scan_session untuk batch konfirmasi ini
        scan_session = ScanSession(
            user_id=user_id,
            store_id=str(request_data.store_id),
            created_at=now_utc
        )
        db.add(scan_session)
        db.flush() # Flush untuk mendapatkan generated ID scan_session
        
        for item in request_data.items:
            product_id = None
            clean_name = item.nama_produk.strip()
            
            # Cek apakah produk dengan nama yang sama sudah ada di tabel products (case-insensitive match)
            # Cek di session's new objects terlebih dahulu untuk menghindari duplikasi dalam batch yang sama
            product = None
            for obj in db.new:
                if isinstance(obj, Product) and obj.nama.lower() == clean_name.lower():
                    product = obj
                    break
            
            if not product:
                product = db.query(Product).filter(Product.nama.ilike(clean_name)).first()
            
            if product:
                # Produk sudah ada, ambil product_id-nya
                product_id = product.id
                # Update category only if the existing category is legacy "General"
                if product.kategori.strip().lower() == "general":
                    product.kategori = item.kategori.value if isinstance(item.kategori, ProductCategory) else item.kategori
            else:
                # Produk belum ada, buat produk baru
                new_product = Product(
                    nama=clean_name,
                    ukuran=item.ukuran,
                    satuan=item.satuan,
                    kategori=item.kategori.value if isinstance(item.kategori, ProductCategory) else item.kategori
                )
                db.add(new_product)
                db.flush() # Flush untuk mendapatkan generated ID dari database
                product_id = new_product.id
                products_created += 1
                
            # Insert ke tabel price_entries untuk tiap item dengan scan_session_id
            price_entry = PriceEntry(
                product_id=product_id,
                store_id=str(request_data.store_id),
                harga=item.harga,
                sumber_user_id=user_id,
                status_verifikasi=VerificationStatus.VERIFIED,
                scan_session_id=scan_session.id,
                timestamp=now_utc
            )
            db.add(price_entry)
            items_saved += 1

            
        # Commit seluruh perubahan sekaligus secara atomic
        db.commit()
        
        message = f"Berhasil menyimpan {items_saved} entri harga. Membuat {products_created} produk baru."
        return ConfirmResponse(
            items_saved=items_saved,
            products_created=products_created,
            message=message,
            scan_session_id=str(scan_session.id)
        )
        
    except Exception as e:
        db.rollback()
        logger.error(f"Error during scan confirmation: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Terjadi kesalahan saat menyimpan konfirmasi: {str(e)}"
        )


@router.get("/recent", response_model=List[RecentScanItem], status_code=status.HTTP_200_OK)
def get_recent_scans(
    limit: int = Query(10, description="Maksimum jumlah scan terbaru yang diambil"),
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
        .order_by(ScanSession.created_at.desc())
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
    )




