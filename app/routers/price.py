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
        # Cast product_id to integer if it is passed as a string
        try:
            prod_id_int = int(price_data.product_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="product_id harus berupa angka integer."
            )

        # Cek apakah produk dengan ID tersebut memang ada
        product_exists = db.query(Product).filter(Product.id == prod_id_int).first()
        if not product_exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Produk dengan ID {prod_id_int} tidak ditemukan."
            )

        # Menyiapkan data insert dengan status_verifikasi default "pending"
        db_entry = PriceEntry(
            product_id=prod_id_int,
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


@router.get("/api/v1/products/{product_id}/price-history", response_model=PriceHistoryResponse)
def get_price_history_v1(
    product_id: int,
    store_id: Optional[str] = Query(None, description="Filter berdasarkan ID toko (opsional)"),
    range: Optional[DateRange] = Query(DateRange.ALL, description="Filter rentang tanggal (1m, 3m, 6m, all)"),
    start_date: Optional[datetime] = Query(None, description="Batas awal tanggal (opsional)"),
    end_date: Optional[datetime] = Query(None, description="Batas akhir tanggal (opsional)"),
    db: Session = Depends(get_db),
):
    """
    Menampilkan histori harga per produk beserta data tren harga untuk grafik (FR-3.2, FR-3.3, FR-3.4).

    Parameters:
    - **product_id**: ID produk (integer)
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
        # Cast product_id to integer
        try:
            prod_id_int = int(product_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="product_id harus berupa angka integer."
            )

        # Inisialisasi query ke tabel price_entries
        query = db.query(PriceEntry).filter(PriceEntry.product_id == prod_id_int)
        
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

