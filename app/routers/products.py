from fastapi import APIRouter, Depends, Query, HTTPException, status, UploadFile, File
from sqlalchemy import func
from sqlalchemy.orm import Session
from typing import List, Optional, Tuple, Union
from pydantic import BaseModel, Field
import logging

from app.database import get_db
from app.models.db_models import Product
from app.models.schemas import MAX_PAGE_SIZE, ProductCategoryType, ProductPhotoUpdate
from app.dependencies import get_current_admin_user
from app.services.storage_upload import upload_image
from app.services.errors import safe_error
from app.routers.stores import haversine_distance, optional_user_location
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
    harga_terendah: Optional[float] = Field(None, description="Harga terendah produk terverifikasi")
    nama_toko_terendah: Optional[str] = Field(None, description="Nama toko yang menjual harga terendah")

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
    jarak_km: Optional[float] = Field(None, description="Jarak (km) dari lat/lng pengguna ke toko harga terendah; null bila lat/lng atau koordinat toko tidak ada")


def _escape_like(text: str) -> str:
    """Escape % dan _ supaya input pengguna dicocokkan secara literal pada ILIKE."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _filter_products(query, search: Optional[str], category: Optional[str]):
    if search and search.strip():
        query = query.filter(Product.nama.ilike(f"%{_escape_like(search.strip())}%", escape="\\"))
    if category and category.strip() and category.strip().lower() != "semua":
        query = query.filter(func.lower(Product.kategori) == category.strip().lower())
    return query


@router.get("/", response_model=List[ProductOut], status_code=status.HTTP_200_OK)
def get_products(
    search: Optional[str] = Query(None, description="Filter berdasarkan nama produk (case-insensitive partial match, literal)"),
    category: Optional[str] = Query(None, description="Filter kategori (cocok persis, tanpa beda huruf besar/kecil; 'semua' = tanpa filter)"),
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE, description="Jumlah produk per halaman (maks 100)"),
    offset: int = Query(0, ge=0, description="Lewati sejumlah produk (urut nama)"),
    db: Session = Depends(get_db)
):
    """
    Mengambil daftar produk dari database lokal, berhalaman (limit/offset), urut nama beserta harga terendah terverifikasi.
    Dapat difilter berdasarkan nama produk ('search') dan/atau kategori ('category').
    """
    from app.models.db_models import Store

    query = _filter_products(db.query(Product), search, category)
    products = query.order_by(Product.nama.asc(), Product.id.asc()).offset(offset).limit(limit).all()
    if not products:
        return []

    # Harga terkini per (produk, toko): terbaru, verified saja
    prices_by_product: dict[str, list[CurrentPrice]] = {}
    for (pid, _sid), price in get_current_prices(db, [prod.id for prod in products]).items():
        prices_by_product.setdefault(pid, []).append(price)
    best_by_product = {pid: pick_cheapest(prices) for pid, prices in prices_by_product.items()}

    store_names: dict[str, str] = {}
    needed_store_ids = {best.store_id for best in best_by_product.values() if best is not None}
    if needed_store_ids:
        for s in db.query(Store).filter(Store.id.in_(needed_store_ids)).all():
            store_names[str(s.id)] = s.nama

    output = []
    for prod in products:
        pid = str(prod.id)
        best = best_by_product.get(pid)
        harga_min = float(best.harga) if best else None
        toko_min = store_names.get(str(best.store_id)) if best else None

        output.append(
            ProductOut(
                id=prod.id,
                nama=prod.nama,
                kategori=prod.kategori,
                ukuran=prod.ukuran,
                satuan=prod.satuan,
                foto_url=getattr(prod, "foto_url", None),
                harga_terendah=harga_min,
                nama_toko_terendah=toko_min,
            )
        )
    return output


@router.get("/catalog", response_model=List[ProductCatalogItem], status_code=status.HTTP_200_OK)
def get_products_catalog(
    search: Optional[str] = Query(None, description="Filter berdasarkan nama produk"),
    category: Optional[str] = Query(None, description="Filter kategori (cocok persis, tanpa beda huruf besar/kecil)"),
    limit: int = Query(100, ge=1, le=MAX_PAGE_SIZE, description="Jumlah produk per halaman (maks 100)"),
    offset: int = Query(0, ge=0, description="Lewati sejumlah produk (urut nama)"),
    location: Optional[Tuple[float, float]] = Depends(optional_user_location),
    db: Session = Depends(get_db)
):
    """
    Mengambil katalog produk terstruktur lengkap dengan harga terendah, toko penyedia, dan info ketersediaan.
    """
    from app.models.db_models import Store

    query = _filter_products(db.query(Product), search, category)
    products = query.order_by(Product.nama.asc(), Product.id.asc()).offset(offset).limit(limit).all()
    if not products:
        return []

    # Harga terkini per (produk, toko): terbaru, verified saja, stale > 90 hari (aturan bersama).
    prices_by_product: dict[str, list[CurrentPrice]] = {}
    for (pid, _sid), price in get_current_prices(db, [prod.id for prod in products]).items():
        prices_by_product.setdefault(pid, []).append(price)
    best_by_product = {pid: pick_cheapest(prices) for pid, prices in prices_by_product.items()}

    stores_by_id: dict[str, Store] = {}
    needed_store_ids = {best.store_id for best in best_by_product.values()}
    if needed_store_ids:
        for s in db.query(Store).filter(Store.id.in_(needed_store_ids)).all():
            stores_by_id[str(s.id)] = s

    def distance_to(store_id: str) -> Optional[float]:
        store = stores_by_id.get(store_id)
        if location is None or store is None or store.lat is None or store.lng is None:
            return None
        return round(haversine_distance(location[0], location[1], store.lat, store.lng), 2)

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
                nama_toko_terendah=stores_by_id[best.store_id].nama if best and best.store_id in stores_by_id else None,
                jumlah_toko=len(prices_by_product.get(pid, [])),
                foto_url=getattr(prod, "foto_url", None),
                updated_at=best.timestamp.isoformat() if best else None,
                stale=best.stale if best else False,
                jarak_km=distance_to(best.store_id) if best else None,
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

    from app.models.db_models import PriceEntry, Store
    from sqlalchemy import func

    pe = (
        db.query(PriceEntry)
        .filter(
            PriceEntry.product_id == product.id,
            func.lower(PriceEntry.status_verifikasi) == "verified"
        )
        .order_by(PriceEntry.harga.asc(), PriceEntry.timestamp.desc())
        .first()
    )

    toko_min = None
    if pe:
        st = db.query(Store).filter(Store.id == pe.store_id).first()
        toko_min = st.nama if st else None

    return ProductOut(
        id=product.id,
        nama=product.nama,
        kategori=product.kategori,
        ukuran=product.ukuran,
        satuan=product.satuan,
        foto_url=getattr(product, "foto_url", None),
        harga_terendah=float(pe.harga) if pe else None,
        nama_toko_terendah=toko_min,
    )


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
