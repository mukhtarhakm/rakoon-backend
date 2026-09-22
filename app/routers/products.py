from fastapi import APIRouter, Depends, Query, HTTPException, status, UploadFile, File
from sqlalchemy.orm import Session
from typing import List, Optional, Union
from pydantic import BaseModel, Field
from pathlib import Path
import uuid
import os

from app.database import get_db
from app.models.db_models import Product
from app.models.schemas import ProductCategoryType, ProductPhotoUpdate
from app.dependencies import get_current_admin_user

from uuid import UUID

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"
UPLOAD_DIR = STATIC_DIR / "uploads" / "products"
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
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Produk dengan ID '{product_id}' tidak ditemukan."
        )

    product.foto_url = payload.foto_url.strip()
    db.commit()
    db.refresh(product)
    return product


@router.post("/{product_id}/upload-photo", response_model=ProductOut, status_code=status.HTTP_200_OK)
async def upload_product_photo(
    product_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    admin_user: dict = Depends(get_current_admin_user)
):
    """
    Mengunggah berkas gambar foto produk secara langsung (Khusus Admin).
    Format yang didukung: JPEG, PNG, WebP. Maksimum 5 MB.
    """
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Produk dengan ID '{product_id}' tidak ditemukan."
        )

    content_type = (file.content_type or "").lower().strip()
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Format file tidak valid. Gunakan format JPEG, PNG, atau WebP."
        )

    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ukuran file terlalu besar. Maksimum 5MB."
        )

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    ext = Path(file.filename or "").suffix.lower()
    if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
        ext = ".jpg" if "jpeg" in content_type else ".png"

    filename = f"{uuid.uuid4().hex}{ext}"
    file_path = UPLOAD_DIR / filename

    with open(file_path, "wb") as f:
        f.write(contents)

    product.foto_url = f"/static/uploads/products/{filename}"
    db.commit()
    db.refresh(product)
    return product
