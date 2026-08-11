from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session
from typing import List, Optional, Union
from pydantic import BaseModel, Field

from app.database import get_db
from app.models.db_models import Product

from uuid import UUID

router = APIRouter()

class ProductOut(BaseModel):
    id: Union[int, str, UUID] = Field(..., description="ID unik dari produk")
    nama: str = Field(..., description="Nama produk")
    kategori: str = Field(..., description="Kategori produk")
    ukuran: Optional[float] = Field(None, description="Ukuran/volume/berat produk")
    satuan: Optional[str] = Field(None, description="Satuan ukuran produk (gr, ml, dll)")

    class Config:
        orm_mode = True
        from_attributes = True

@router.get("/", response_model=List[ProductOut], status_code=status.HTTP_200_OK)
def get_products(
    search: Optional[str] = Query(None, description="Filter berdasarkan nama produk (case-insensitive partial match)"),
    limit: int = Query(20, description="Maksimum jumlah produk yang dikembalikan"),
    db: Session = Depends(get_db)
):
    """
    Mengambil daftar produk dari database lokal.
    Jika query parameter 'search' diberikan, akan mencari produk secara partial match (case-insensitive).
    Hasil diurutkan secara alfabetis berdasarkan nama produk.
    """
    query = db.query(Product)
    
    if search and search.strip():
        query = query.filter(Product.nama.ilike(f"%{search.strip()}%"))
        
    products = query.order_by(Product.nama.asc()).limit(limit).all()
    return products
