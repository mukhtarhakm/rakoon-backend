from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, Union, List
from uuid import UUID

class PriceEntryCreate(BaseModel):
    product_id: Union[int, str, UUID] = Field(..., description="ID of the product")
    store_id: Union[int, str, UUID] = Field(..., description="ID of the store")
    harga: int = Field(..., description="Price value of the product at the store")
    sumber_user_id: Union[int, str, UUID] = Field(..., description="ID of the user submitting the price")

class PriceEntryOut(PriceEntryCreate):
    id: Union[int, str, UUID] = Field(..., description="ID of the price entry")
    timestamp: datetime = Field(..., description="Timestamp of when the price entry was created")
    status_verifikasi: str = Field(..., description="Verification status of the price entry (e.g., 'pending')")

class ScanResultItem(BaseModel):
    nama_produk: Optional[str] = Field(None, description="Nama produk yang terdeteksi")
    harga: Optional[float] = Field(None, description="Harga produk (angka, null jika tidak terbaca)")
    ukuran: Optional[float] = Field(None, description="Ukuran produk (angka, null jika tidak terbaca)")
    satuan: Optional[str] = Field(None, description="Satuan ukuran produk (null jika tidak terbaca)")
    confidence: str = Field(..., description="Tingkat kepercayaan ('tinggi' atau 'rendah')")
    needs_verification: bool = Field(False, description="Menandakan apakah item butuh verifikasi manual")

class ScanResponse(BaseModel):
    detected: List[ScanResultItem] = Field(default_factory=list, description="Daftar produk yang terdeteksi")
    message: Optional[str] = Field(None, description="Pesan tambahan (misal jika tidak ada produk terdeteksi)")

class ConfirmItem(BaseModel):
    nama_produk: str = Field(..., description="Nama produk yang dikonfirmasi")
    harga: int = Field(..., description="Harga produk hasil konfirmasi/koreksi")
    ukuran: Optional[float] = Field(None, description="Ukuran produk (nullable)")
    satuan: Optional[str] = Field(None, description="Satuan ukuran produk (nullable)")

class ConfirmRequest(BaseModel):
    store_id: Union[int, str, UUID] = Field(..., description="ID dari toko tempat scan dilakukan")
    user_id: Union[int, str, UUID] = Field(..., description="ID dari pengguna yang melakukan konfirmasi")
    items: List[ConfirmItem] = Field(..., description="Daftar item hasil scan yang dikonfirmasi")

class ConfirmResponse(BaseModel):
    items_saved: int = Field(..., description="Jumlah entri harga yang berhasil disimpan")
    products_created: int = Field(..., description="Jumlah produk baru yang berhasil dibuat")
    message: str = Field(..., description="Pesan status konfirmasi")

class PriceCompareItem(BaseModel):
    nama_toko: str = Field(..., description="Nama toko")
    jarak_km: float = Field(..., description="Jarak dari titik user dalam km")
    harga_terbaru: Optional[int] = Field(None, description="Harga terbaru produk di toko ini")
    tanggal_update: Optional[datetime] = Field(None, description="Tanggal update harga terbaru")
    status_verifikasi: Optional[str] = Field(None, description="Status verifikasi harga")
    pesan: Optional[str] = Field(None, description="Pesan status ketersediaan harga")

class PriceCompareResponse(BaseModel):
    product_id: Union[int, str] = Field(..., description="ID dari produk")
    nama_produk: str = Field(..., description="Nama produk")
    comparison: List[PriceCompareItem] = Field(default_factory=list, description="Daftar perbandingan harga di toko terdekat")
