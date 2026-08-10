from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, Union, List

class PriceEntryCreate(BaseModel):
    product_id: Union[int, str] = Field(..., description="ID of the product")
    store_id: Union[int, str] = Field(..., description="ID of the store")
    harga: int = Field(..., description="Price value of the product at the store")
    sumber_user_id: Union[int, str] = Field(..., description="ID of the user submitting the price")

class PriceEntryOut(PriceEntryCreate):
    id: Union[int, str] = Field(..., description="ID of the price entry")
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
    store_id: Union[int, str] = Field(..., description="ID dari toko tempat scan dilakukan")
    user_id: Union[int, str] = Field(..., description="ID dari pengguna yang melakukan konfirmasi")
    items: List[ConfirmItem] = Field(..., description="Daftar item hasil scan yang dikonfirmasi")

class ConfirmResponse(BaseModel):
    items_saved: int = Field(..., description="Jumlah entri harga yang berhasil disimpan")
    products_created: int = Field(..., description="Jumlah produk baru yang berhasil dibuat")
    message: str = Field(..., description="Pesan status konfirmasi")


# ---------------------------------------------------------------------------
# F3 — Price History Schemas
# ---------------------------------------------------------------------------

class PriceHistoryItem(BaseModel):
    """Satu baris riwayat harga dari tabel price_entries."""

    id: int = Field(..., description="ID entri harga")
    product_id: int = Field(..., description="ID produk")
    store_id: str = Field(..., description="ID toko tempat harga dicatat")
    harga: int = Field(..., description="Harga produk (satuan: Rupiah)")
    sumber_user_id: str = Field(..., description="ID user yang menginput harga")
    # ORM column name is `timestamp`; exposed publicly as `recorded_at`
    recorded_at: datetime = Field(..., alias="timestamp", description="Waktu harga dicatat")
    status_verifikasi: str = Field(..., description="Status verifikasi entri ('pending' / 'verified')")

    model_config = {"from_attributes": True, "populate_by_name": True}



class PriceTrendPoint(BaseModel):
    """Satu titik data untuk grafik tren harga."""

    date: str = Field(..., description="Tanggal dalam format YYYY-MM-DD")
    store_id: str = Field(..., description="ID toko")
    price: int = Field(..., description="Harga (atau rata-rata harga jika ada beberapa entri pada hari yang sama)")


class PriceHistoryResponse(BaseModel):
    """Response wrapper untuk daftar riwayat harga satu produk."""

    product_id: int = Field(..., description="ID produk yang diminta")
    total: int = Field(..., description="Jumlah total entri yang dikembalikan")
    items: List[PriceHistoryItem] = Field(default_factory=list, description="Daftar entri riwayat harga")
    trend: List[PriceTrendPoint] = Field(default_factory=list, description="Daftar titik data grafik tren harga")

