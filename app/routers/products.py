from fastapi import APIRouter, Depends, Query, HTTPException, status, UploadFile, File
from sqlalchemy.orm import Session
from typing import List, Optional, Union
from pydantic import BaseModel, Field
from pathlib import Path
import uuid
import os
import logging

from app.database import get_db, supabase
from app.models.db_models import Product
from app.models.schemas import ProductCategoryType, ProductPhotoUpdate
from app.dependencies import get_current_admin_user

from uuid import UUID

logger = logging.getLogger("rakoon_backend.products")
router = APIRouter()

SUPABASE_BUCKET = "product-photos"
STATIC_UPLOADS_DIR = Path(__file__).resolve().parent.parent.parent / "static" / "uploads" / "products"
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/jpg"}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB

class ProductOut(BaseModel):
    id: Union[int, str, UUID] = Field(..., description="ID unik dari produk")
    nama: str = Field(..., description="Nama produk")
    kategori: ProductCategoryType = Field(..., description="Kategori produk")
    ukuran: Optional[float] = Field(None, description="Ukuran/volume/berat produk")
    satuan: Optional[str] = Field(None, description="Satuan ukuran produk (gr, ml, dll)")
    foto_url: Optional[str] = Field(None, description="URL foto produk")

    model_config = {"from_attributes": True}


class ProductCatalogItem(BaseModel):
    id: str = Field(..., description="ID produk")
    nama: str = Field(..., description="Nama produk")
    kategori: str = Field(..., description="Kategori produk")
    ukuran: Optional[float] = Field(None, description="Ukuran/volume/berat produk")
    satuan: Optional[str] = Field(None, description="Satuan ukuran produk")
    harga_terendah: Optional[float] = Field(None, description="Harga terendah produk dari toko sekitar")
    nama_toko_terendah: Optional[str] = Field(None, description="Nama toko yang menjual harga terendah")
    jumlah_toko: int = Field(0, description="Jumlah toko yang menyediakan produk ini")
    foto_url: Optional[str] = Field(None, description="URL foto produk")
    updated_at: Optional[str] = Field(None, description="Timestamp ISO entri harga terbaru")


@router.get("/", response_model=List[ProductOut], status_code=status.HTTP_200_OK)
def get_products(
    search: Optional[str] = Query(None, description="Filter berdasarkan nama produk (case-insensitive partial match)"),
    category: Optional[str] = Query(None, description="Filter berdasarkan kategori produk"),
    limit: int = Query(50, description="Maksimum jumlah produk yang dikembalikan"),
    db: Session = Depends(get_db)
):
    """
    Mengambil daftar produk dari database lokal.
    Dapat difilter berdasarkan nama produk ('search') dan/atau kategori ('category').
    """
    query = db.query(Product)
    
    if search and search.strip():
        query = query.filter(Product.nama.ilike(f"%{search.strip()}%"))
        
    if category and category.strip() and category.strip().lower() != "semua":
        query = query.filter(Product.kategori.ilike(f"%{category.strip()}%"))
        
    products = query.order_by(Product.nama.asc()).limit(limit).all()
    return products


@router.get("/catalog", response_model=List[ProductCatalogItem], status_code=status.HTTP_200_OK)
def get_products_catalog(
    search: Optional[str] = Query(None, description="Filter berdasarkan nama produk"),
    category: Optional[str] = Query(None, description="Filter berdasarkan kategori produk"),
    limit: int = Query(100, description="Maksimum jumlah produk katalog"),
    db: Session = Depends(get_db)
):
    """
    Mengambil katalog produk terstruktur lengkap dengan harga terendah, toko penyedia, dan info ketersediaan.
    """
    from app.models.db_models import PriceEntry, Store

    query = db.query(Product)

    if search and search.strip():
        query = query.filter(Product.nama.ilike(f"%{search.strip()}%"))

    if category and category.strip() and category.strip().lower() != "semua":
        query = query.filter(Product.kategori.ilike(f"%{category.strip()}%"))

    products = query.order_by(Product.nama.asc()).limit(limit).all()
    if not products:
        return []

    # Batch fetch all relevant PriceEntry rows in a single query (eliminating N+1)
    product_ids = [prod.id for prod in products]
    price_entries = (
        db.query(PriceEntry)
        .filter(
            PriceEntry.product_id.in_(product_ids),
            PriceEntry.status_verifikasi != "rejected"
        )
        .order_by(PriceEntry.harga.asc(), PriceEntry.timestamp.desc())
        .all()
    )

    pes_by_product: dict[str, list[PriceEntry]] = {}
    needed_store_ids = set()
    for pe in price_entries:
        pid_str = str(pe.product_id)
        if pid_str not in pes_by_product:
            pes_by_product[pid_str] = []
            needed_store_ids.add(pe.store_id)
        pes_by_product[pid_str].append(pe)

    # Batch fetch cheapest store names in a single query
    store_names: dict[str, str] = {}
    if needed_store_ids:
        stores = db.query(Store).filter(Store.id.in_(needed_store_ids)).all()
        for s in stores:
            store_names[str(s.id)] = s.nama

    catalog_items: List[ProductCatalogItem] = []

    for prod in products:
        pes = pes_by_product.get(str(prod.id), [])
        harga_min = None
        toko_min = None
        ts_latest = None
        store_ids = set()

        if pes:
            cheapest = pes[0]
            harga_min = float(cheapest.harga)
            if cheapest.timestamp:
                ts_latest = cheapest.timestamp.isoformat()

            toko_min = store_names.get(str(cheapest.store_id))

            for pe in pes:
                store_ids.add(pe.store_id)

        catalog_items.append(
            ProductCatalogItem(
                id=str(prod.id),
                nama=prod.nama,
                kategori=prod.kategori or "General",
                ukuran=prod.ukuran,
                satuan=prod.satuan,
                harga_terendah=harga_min,
                nama_toko_terendah=toko_min,
                jumlah_toko=len(store_ids),
                foto_url=getattr(prod, "foto_url", None),
                updated_at=ts_latest
            )
        )

    return catalog_items


@router.put("/{product_id}/photo", response_model=ProductOut, status_code=status.HTTP_200_OK)
@router.patch("/{product_id}/photo", response_model=ProductOut, status_code=status.HTTP_200_OK)
def update_product_photo_url(
    product_id: str,
    payload: ProductPhotoUpdate,
    db: Session = Depends(get_db),
    admin_user: dict = Depends(get_current_admin_user)
):
    """
    Mengubah atau memperbarui URL foto produk (Khusus Admin).
    """
    try:
        product = db.query(Product).filter(Product.id == product_id).first()
    except Exception:
        db.rollback()
        product = None

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Produk dengan ID '{product_id}' tidak ditemukan."
        )

    product.foto_url = payload.foto_url.strip()
    db.commit()
    db.refresh(product)
    return product


def detect_image_type(
    filename: Optional[str],
    declared_content_type: Optional[str],
    contents: bytes
) -> tuple[str, str]:
    """
    Mendeteksi tipe MIME dan ekstensi gambar berdasarkan signature bytes (magic numbers),
    ekstensi berkas, atau declared content-type dari multipart request.
    Mengembalikan (normalized_content_type, extension) seperti ("image/jpeg", ".jpg").
    Melemparkan HTTPException(400) jika bukan gambar valid.
    """
    raw_ct = (declared_content_type or "").lower().split(";")[0].strip()
    ext = Path(filename or "").suffix.lower()

    # 1. Deteksi melalui magic bytes (signature file sebenarnya)
    if contents.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if contents.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if contents.startswith(b"RIFF") and len(contents) >= 12 and contents[8:12] == b"WEBP":
        return "image/webp", ".webp"

    # 2. Deteksi melalui ekstensi file (misalnya .jpg, .jpeg, .png, .webp)
    if ext in [".jpg", ".jpeg"]:
        return "image/jpeg", ".jpg"
    if ext == ".png":
        return "image/png", ".png"
    if ext == ".webp":
        return "image/webp", ".webp"

    # 3. Deteksi melalui declared content_type jika ekstensi / signature belum matched
    if raw_ct in ["image/jpeg", "image/jpg", "image/pjpeg"]:
        return "image/jpeg", ".jpg"
    if raw_ct in ["image/png", "image/x-png"]:
        return "image/png", ".png"
    if raw_ct == "image/webp":
        return "image/webp", ".webp"

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Format file tidak valid. Gunakan format JPEG, PNG, atau WebP."
    )


@router.post("/{product_id}/upload-photo", response_model=ProductOut, status_code=status.HTTP_200_OK)
async def upload_product_photo(
    product_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(get_current_admin_user)
):
    """
    Mengunggah berkas gambar foto produk ke Supabase Storage (Khusus Admin).
    Jika Supabase Storage tidak tersedia, otomatis fallback ke penyimpanan lokal /static/uploads/products/.
    Format yang didukung: JPEG, PNG, WebP. Maksimum 5 MB.
    """
    try:
        product = db.query(Product).filter(Product.id == product_id).first()
    except Exception:
        db.rollback()
        product = None

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Produk dengan ID '{product_id}' tidak ditemukan."
        )

    contents = await file.read()
    if not contents:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Berkas foto yang diunggah kosong."
        )

    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ukuran file terlalu besar. Maksimum 5MB."
        )

    # Validasi dan normalisasi tipe konten serta ekstensi berkas
    content_type, ext = detect_image_type(
        filename=file.filename,
        declared_content_type=file.content_type,
        contents=contents
    )

    filename = f"{uuid.uuid4().hex}{ext}"
    photo_url: Optional[str] = None
    upload_error: Optional[str] = None

    # 1. Coba simpan ke Supabase Storage jika client aktif
    if hasattr(supabase, "storage"):
        try:
            supabase.storage.from_(SUPABASE_BUCKET).upload(
                filename,
                contents,
                {"content-type": content_type, "upsert": "true"}
            )
            raw_url = supabase.storage.from_(SUPABASE_BUCKET).get_public_url(filename)
            photo_url = raw_url.rstrip("?") if raw_url else None
        except Exception as exc:
            upload_error = str(exc)
            logger.warning(f"Gagal mengunggah ke Supabase Storage ({exc}). Beralih ke penyimpanan statis lokal.")

    # 2. Fallback ke penyimpanan statis lokal jika Supabase Storage gagal atau belum dikonfigurasi
    if not photo_url:
        try:
            STATIC_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
            local_file_path = STATIC_UPLOADS_DIR / filename
            with open(local_file_path, "wb") as f:
                f.write(contents)
            photo_url = f"/static/uploads/products/{filename}"
            logger.info(f"Foto berhasil disimpan secara lokal: {photo_url}")
        except Exception as local_err:
            logger.error(f"Penyimpanan berkas lokal juga gagal: {local_err}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Gagal menyimpan foto produk: {upload_error or str(local_err)}"
            )

    product.foto_url = photo_url
    db.commit()
    db.refresh(product)
    return product
