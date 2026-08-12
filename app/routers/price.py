import re
from fastapi import APIRouter, HTTPException, Query, status, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from typing import List, Union, Optional
from datetime import datetime
from app.models.schemas import PriceEntryCreate, PriceEntryOut, PriceHistoryResponse, PriceCompareResponse, PriceCompareItem, VerificationStatus
from app.models.db_models import PriceEntry, Product
from app.database import get_db
from app.services import price_history_service
from app.services.price_history_service import DateRange
from app.routers.stores import get_nearby_stores

router = APIRouter()

def parse_product_id(product_id: str) -> Union[int, str]:
    """
    Memparse product_id secara fleksibel.
    Mengembalikan integer jika memungkinkan, atau string UUID yang valid.
    Mengangkat ValueError jika format tidak valid.
    """
    if not product_id:
        raise ValueError("product_id tidak boleh kosong.")
    
    # Coba parsing sebagai integer (SQLite legacy support)
    try:
        return int(product_id)
    except ValueError:
        pass
    
    # Cek apakah string merupakan UUID yang valid (8-4-4-4-12 hex chars)
    if re.match(r'^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$', product_id):
        return product_id
        
    raise ValueError("product_id harus berupa angka integer atau UUID yang valid.")

@router.post("/", response_model=PriceEntryOut, status_code=status.HTTP_201_CREATED)
def create_price_entry(price_data: PriceEntryCreate, db: Session = Depends(get_db)):
    """
    Simpan entri harga baru ke database.
    Status verifikasi default diatur menjadi "pending".
    """
    try:
        # Validasi dan parse product_id (mendukung Integer maupun UUID)
        try:
            prod_id_parsed = parse_product_id(str(price_data.product_id))
        except ValueError as ve:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(ve)
            )

        # Cek apakah produk dengan ID tersebut memang ada
        product_exists = db.query(Product).filter(Product.id == prod_id_parsed).first()
        if not product_exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Produk dengan ID {price_data.product_id} tidak ditemukan."
            )

        # Menyiapkan data insert dengan status_verifikasi default "verified"
        db_entry = PriceEntry(
            product_id=prod_id_parsed,
            store_id=str(price_data.store_id),
            harga=price_data.harga,
            sumber_user_id=str(price_data.sumber_user_id),
            status_verifikasi=VerificationStatus.VERIFIED
        )
        
        db.add(db_entry)
        db.commit()
        db.refresh(db_entry)
        
        return db_entry
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Terjadi kesalahan saat menyimpan data harga: {str(e)}"
        )


PRICE_HISTORY_RESPONSES = {
    200: {
        "description": "Riwayat harga dan data tren grafik berhasil diambil.",
        "content": {
            "application/json": {
                "example": {
                    "product_id": "1",
                    "total": 2,
                    "items": [
                        {
                            "id": "101",
                            "product_id": "1",
                            "store_id": "store_001",
                            "harga": 18000,
                            "sumber_user_id": "user_123",
                            "recorded_at": "2026-08-05T10:00:00Z",
                            "status_verifikasi": "verified",
                        },
                        {
                            "id": "102",
                            "product_id": "1",
                            "store_id": "store_001",
                            "harga": 19000,
                            "sumber_user_id": "user_456",
                            "recorded_at": "2026-08-05T14:30:00Z",
                            "status_verifikasi": "pending",
                        },
                    ],
                    "trend": [
                        {
                            "date": "2026-08-05",
                            "store_id": "store_001",
                            "price": 18500,
                        }
                    ],
                }
            }
        },
    },
    404: {
        "description": "Produk tidak ditemukan di database.",
        "content": {
            "application/json": {
                "example": {"detail": "Produk dengan ID 999 tidak ditemukan."}
            }
        },
    },
}


@router.get(
    "/api/v1/products/{product_id}/price-history",
    response_model=PriceHistoryResponse,
    summary="Get Price History and Trend Data for a Product",
    description=(
        "Mengambil riwayat entri harga mentah dan data tren harga ter-agregasi (chart-ready) "
        "untuk produk tertentu berdasarkan `product_id`. Mendukung filter berdasarkan toko (`store_id`) "
        "dan rentang tanggal preset (`1m`, `3m`, `6m`, `all`) atau rentang tanggal kustom.\n\n"
        "**Persyaratan Functional (FR):**\n"
        "- **FR-3.2**: Menampilkan histori harga per produk.\n"
        "- **FR-3.3**: Filter histori berdasarkan toko dan rentang tanggal.\n"
        "- **FR-3.4**: Menampilkan data untuk grafik tren harga (rata-rata harga per hari per toko)."
    ),
    responses=PRICE_HISTORY_RESPONSES,
    tags=["Price History"],
)
def get_price_history_v1(
    product_id: str,
    store_id: Optional[str] = Query(None, description="Filter berdasarkan ID toko (opsional, contoh: 'store_001')"),
    range: Optional[DateRange] = Query(DateRange.ALL, description="Filter rentang tanggal ('1m', '3m', '6m', 'all')"),
    start_date: Optional[datetime] = Query(None, description="Batas awal tanggal (opsional, ISO 8601 format)"),
    end_date: Optional[datetime] = Query(None, description="Batas akhir tanggal (opsional, ISO 8601 format)"),
    db: Session = Depends(get_db),
):
    """
    Menampilkan histori harga per produk beserta data tren harga untuk grafik (FR-3.2, FR-3.3, FR-3.4).

    Parameters:
    - **product_id**: ID produk (string atau integer)
    - **store_id**: Filter berdasarkan toko (opsional)
    - **range**: Preset rentang tanggal ('1m', '3m', '6m', 'all')
    - **start_date** & **end_date**: Batas tanggal kustom (opsional)
    """
    try:
        return price_history_service.get_price_history(
            db,
            product_id,
            store_id=store_id,
            range_enum=range,
            start_date=start_date,
            end_date=end_date,
        )
    except ValueError as ve:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(ve),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Terjadi kesalahan saat mengambil riwayat harga: {str(e)}",
        )



@router.get("/product/{product_id}")
def get_price_history(
    product_id: str,
    store_id: Optional[str] = Query(None, description="Filter berdasarkan ID toko (opsional)"),
    db: Session = Depends(get_db)
):
    """
    Ambil riwayat harga produk tertentu, diurutkan dari yang terbaru (timestamp DESC).
    Jika data kosong, mengembalikan pesan 'Belum ada data historis'.
    """
    try:
        # Validasi dan parse product_id (mendukung Integer maupun UUID)
        try:
            prod_id_parsed = parse_product_id(product_id)
        except ValueError as ve:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(ve)
            )

        # Inisialisasi query ke tabel price_entries
        query = db.query(PriceEntry).filter(PriceEntry.product_id == prod_id_parsed)
        
        # Filter store_id jika disediakan
        if store_id:
            query = query.filter(PriceEntry.store_id == store_id)
            
        # Urutkan berdasarkan timestamp descending (terbaru dahulu)
        records = query.order_by(PriceEntry.timestamp.desc()).all()
        
        # Jika data kosong, kembalikan pesan "Belum ada data historis" dengan status 200 OK
        if not records:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={"message": "Belum ada data historis"}
            )
            
        # Pydantic/FastAPI will serialize list of PriceEntry models automatically
        return records
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Terjadi kesalahan saat mengambil riwayat harga: {str(e)}"
        )

@router.get("/compare/{product_id}", response_model=PriceCompareResponse)
async def get_price_comparison(
    product_id: str,
    lat: float = Query(..., description="Latitude koordinat pengguna"),
    lng: float = Query(..., description="Longitude koordinat pengguna"),
    radius_km: float = Query(5.0, description="Radius pencarian dalam kilometer"),
    db: Session = Depends(get_db)
):
    """
    Membandingkan harga terupdate dari suatu produk di toko-toko terdekat.
    Mengambil daftar toko terdekat dan mencari harga terbaru untuk product_id tersebut.
    Mengurutkan toko berdasarkan harga termurah.
    """
    try:
        # 1. Parsing product_id secara fleksibel (ke integer jika memungkinkan, atau tetap string jika UUID)
        try:
            prod_id_parsed = parse_product_id(product_id)
        except ValueError as ve:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(ve)
            )

        # 2. Cek apakah produk dengan ID tersebut memang ada
        product = db.query(Product).filter(Product.id == prod_id_parsed).first()
        if not product:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Produk dengan ID {product_id} tidak ditemukan."
            )

        # 3. Cari toko di sekitar menggunakan logic dari GET /stores/nearby
        stores_response = await get_nearby_stores(lat=lat, lng=lng, radius_km=radius_km, db=db)
        nearby_stores = stores_response.stores

        # 4. Ambil harga terbaru dari tiap toko
        comparison_list = []
        for store in nearby_stores:
            # Query harga terupdate (order by timestamp DESC, mengecualikan 'rejected')
            latest_entry = db.query(PriceEntry).filter(
                PriceEntry.product_id == prod_id_parsed,
                PriceEntry.store_id == store.store_id,
                PriceEntry.status_verifikasi != VerificationStatus.REJECTED
            ).order_by(PriceEntry.timestamp.desc()).first()

            if latest_entry:
                comparison_list.append(
                    PriceCompareItem(
                        nama_toko=store.nama,
                        jarak_km=store.jarak_km,
                        harga_terbaru=latest_entry.harga,
                        tanggal_update=latest_entry.timestamp,
                        status_verifikasi=latest_entry.status_verifikasi,
                        pesan=None
                    )
                )
            else:
                # Jika tidak ada data harga, tetap tampilkan dengan harga null dan keterangan
                comparison_list.append(
                    PriceCompareItem(
                        nama_toko=store.nama,
                        jarak_km=store.jarak_km,
                        harga_terbaru=None,
                        tanggal_update=None,
                        status_verifikasi=None,
                        pesan="Belum ada data untuk produk ini di toko ini"
                    )
                )

        # 5. Urutkan: harga termurah dahulu (harga terendah -> tertinggi), data kosong (harga None) ditaruh di akhir.
        # Jika harga sama, diurutkan berdasarkan jarak terdekat.
        comparison_list.sort(
            key=lambda x: (
                x.harga_terbaru is None,
                x.harga_terbaru if x.harga_terbaru is not None else 0,
                x.jarak_km
            )
        )

        return PriceCompareResponse(
            product_id=prod_id_parsed,
            nama_produk=product.nama,
            comparison=comparison_list
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Terjadi kesalahan saat memproses perbandingan harga: {str(e)}"
        )
