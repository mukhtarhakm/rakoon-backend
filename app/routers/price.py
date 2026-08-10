from fastapi import APIRouter, HTTPException, Query, status, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from typing import List, Union, Optional
from datetime import datetime

from app.models.schemas import PriceEntryCreate, PriceEntryOut, PriceHistoryResponse
from app.models.db_models import PriceEntry, Product
from app.database import get_db
from app.services import price_history_service
from app.services.price_history_service import DateRange

router = APIRouter()

@router.post("/", response_model=PriceEntryOut, status_code=status.HTTP_201_CREATED)
def create_price_entry(price_data: PriceEntryCreate, db: Session = Depends(get_db)):
    """
    Simpan entri harga baru ke database.
    Status verifikasi default diatur menjadi "pending".
    """
    try:
        prod_id_str = str(price_data.product_id)

        # Cek apakah produk dengan ID tersebut memang ada
        product_exists = db.query(Product).filter(Product.id == prod_id_str).first()
        if not product_exists:
            # Buat produk otomatis jika belum ada di tabel products
            new_product = Product(id=prod_id_str, nama=f"Produk {prod_id_str}")
            db.add(new_product)
            db.commit()

        # Menyiapkan data insert dengan status_verifikasi default "pending"
        db_entry = PriceEntry(
            product_id=prod_id_str,
            store_id=str(price_data.store_id),
            harga=price_data.harga,
            sumber_user_id=str(price_data.sumber_user_id),
            status_verifikasi="pending"
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
        prod_id_str = str(product_id)

        # Inisialisasi query ke tabel price_entries
        query = db.query(PriceEntry).filter(PriceEntry.product_id == prod_id_str)
        
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

