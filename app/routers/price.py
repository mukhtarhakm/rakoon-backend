from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import JSONResponse
from typing import List, Union, Optional
from app.models.schemas import PriceEntryCreate, PriceEntryOut
from app.database import supabase
from postgrest.exceptions import APIError

router = APIRouter()

@router.post("/", response_model=PriceEntryOut, status_code=status.HTTP_201_CREATED)
def create_price_entry(price_data: PriceEntryCreate):
    """
    Simpan entri harga baru ke database Supabase.
    Status verifikasi default diatur menjadi "pending".
    """
    try:
        # Menyiapkan data insert dengan status_verifikasi default "pending"
        insert_data = price_data.model_dump()
        insert_data["status_verifikasi"] = "pending"
        
        # Eksekusi insert ke tabel price_entries
        response = supabase.table("price_entries").insert(insert_data).execute()
        
        if not response.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Gagal menyimpan data harga ke database."
            )
            
        return response.data[0]
        
    except APIError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Database error: {e.message}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Terjadi kesalahan: {str(e)}"
        )

@router.get("/product/{product_id}")
def get_price_history(
    product_id: str,
    store_id: Optional[str] = Query(None, description="Filter berdasarkan ID toko (opsional)")
):
    """
    Ambil riwayat harga produk tertentu, diurutkan dari yang terbaru (timestamp DESC).
    Jika data kosong, mengembalikan pesan 'Belum ada data historis'.
    """
    try:
        # Inisialisasi query ke tabel price_entries
        query = supabase.table("price_entries").select("*").eq("product_id", product_id)
        
        # Filter store_id jika disediakan
        if store_id:
            query = query.eq("store_id", store_id)
            
        # Urutkan berdasarkan timestamp descending (terbaru dahulu)
        response = query.order("timestamp", desc=True).execute()
        
        # Jika data kosong, kembalikan pesan "Belum ada data historis" dengan status 200 OK (atau 404 jika diinginkan)
        # Kami menggunakan status 200 OK dengan pesan terstruktur agar tidak memicu crash di sisi client
        if not response.data:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={"message": "Belum ada data historis"}
            )
            
        return response.data
        
    except APIError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Database error: {e.message}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Terjadi kesalahan: {str(e)}"
        )
