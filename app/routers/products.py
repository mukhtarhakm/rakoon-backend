from fastapi import APIRouter, Depends, Query, HTTPException, status, UploadFile, File
from sqlalchemy.orm import Session
from typing import List, Optional, Union
from pydantic import BaseModel, Field
import logging

from app.database import get_db
from app.models.db_models import Product
from app.models.schemas import ProductCategoryType, ProductPhotoUpdate
from app.dependencies import get_current_admin_user
from app.services.storage_upload import upload_image
from app.services.errors import safe_error
from app.services.current_price import CurrentPrice, get_current_prices, pick_cheapest

from uuid import UUID

logger = logging.getLogger("rakoon_backend.products")
router = APIRouter()


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
    updated_at: Optional[str] = Field(None, description="Timestamp ISO harga yang ditampilkan (harga terkini)")
    stale: bool = Field(False, description="True bila harga yang ditampilkan berumur lebih dari 90 hari")


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
    from app.models.db_models import Store

    query = db.query(Product)

    if search and search.strip():
        query = query.filter(Product.nama.ilike(f"%{search.strip()}%"))

    if category and category.strip() and category.strip().lower() != "semua":
        query = query.filter(Product.kategori.ilike(f"%{category.strip()}%"))

    products = query.order_by(Product.nama.asc()).limit(limit).all()
    if not products:
        return []

    # Harga terkini per (produk, toko): terbaru, verified saja, stale > 90 hari (aturan bersama).
    prices_by_product: dict[str, list[CurrentPrice]] = {}
    for (pid, _sid), price in get_current_prices(db, [prod.id for prod in products]).items():
        prices_by_product.setdefault(pid, []).append(price)
    best_by_product = {pid: pick_cheapest(prices) for pid, prices in prices_by_product.items()}

    store_names: dict[str, str] = {}
    needed_store_ids = {best.store_id for best in best_by_product.values()}
    if needed_store_ids:
        for s in db.query(Store).filter(Store.id.in_(needed_store_ids)).all():
            store_names[str(s.id)] = s.nama

    catalog_items: List[ProductCatalogItem] = []
    for prod in products:
        pid = str(prod.id)
        best = best_by_product.get(pid)
        catalog_items.append(
            ProductCatalogItem(
                id=pid,
                nama=prod.nama,
                kategori=prod.kategori or "General",
                ukuran=prod.ukuran,
                satuan=prod.satuan,
                harga_terendah=float(best.harga) if best else None,
                nama_toko_terendah=store_names.get(best.store_id) if best else None,
                jumlah_toko=len(prices_by_product.get(pid, [])),
                foto_url=getattr(prod, "foto_url", None),
                updated_at=best.timestamp.isoformat() if best else None,
                stale=best.stale if best else False,
            )
        )

    return catalog_items


@router.get("/{product_id}", response_model=ProductOut, status_code=status.HTTP_200_OK)
def get_product_by_id(product_id: str, db: Session = Depends(get_db)):
    """
    Mengambil data produk spesifik berdasarkan product_id (termasuk foto_url).
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
    return product


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
    try:
        db.commit()
        db.refresh(product)
        return product
    except Exception as e:
        db.rollback()
        raise safe_error(
            logger,
            e,
            "Gagal memperbarui foto produk.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            log_prefix="Error updating product photo URL",
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
    Format yang didukung: JPEG, PNG, WebP (dicek dari isi berkas). Maksimum 5 MB (413 bila lebih).
    Storage gagal -> 503/502; tidak ada fallback lokal.
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

    product.foto_url = await upload_image(file)
    try:
        db.commit()
        db.refresh(product)
        return product
    except Exception as e:
        db.rollback()
        raise safe_error(
            logger,
            e,
            "Gagal menyimpan foto produk ke database.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            log_prefix="Error saving product photo to database",
        )
