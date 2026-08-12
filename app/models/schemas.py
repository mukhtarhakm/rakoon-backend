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

# ---------------------------------------------------------------------------
# F3 — Price History Schemas
# ---------------------------------------------------------------------------

class PriceHistoryItem(BaseModel):
    """Satu baris riwayat harga dari tabel price_entries."""

    id: Union[UUID, int, str] = Field(..., description="ID entri harga")
    product_id: Union[UUID, int, str] = Field(..., description="ID produk")
    store_id: Union[UUID, str] = Field(..., description="ID toko tempat harga dicatat")
    harga: int = Field(..., description="Harga produk (satuan: Rupiah)")
    sumber_user_id: Union[UUID, str] = Field(..., description="ID user yang menginput harga")
    # ORM column name is `timestamp`; exposed publicly as `recorded_at`
    recorded_at: datetime = Field(..., alias="timestamp", description="Waktu harga dicatat")
    status_verifikasi: str = Field(..., description="Status verifikasi entri ('pending' / 'verified')")

    model_config = {"from_attributes": True, "populate_by_name": True}



class PriceTrendPoint(BaseModel):
    """Satu titik data untuk grafik tren harga."""

    date: str = Field(..., description="Tanggal dalam format YYYY-MM-DD")
    store_id: Union[UUID, str] = Field(..., description="ID toko")
    price: int = Field(..., description="Harga (atau rata-rata harga jika ada beberapa entri pada hari yang sama)")


class PriceHistoryResponse(BaseModel):
    """Response wrapper untuk daftar riwayat harga satu produk."""

    product_id: Union[UUID, int, str] = Field(..., description="ID produk yang diminta")
    total: int = Field(..., description="Jumlah total entri yang dikembalikan")
    items: List[PriceHistoryItem] = Field(default_factory=list, description="Daftar entri riwayat harga")
    trend: List[PriceTrendPoint] = Field(default_factory=list, description="Daftar titik data grafik tren harga")


# ---------------------------------------------------------------------------
# F2 — Price Compare Schemas
# ---------------------------------------------------------------------------

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

# ==============================================================================
# FEATURE 2: BEST VALUE RECOMMENDATION SCHEMAS
# ==============================================================================

class RecommendationCandidate(BaseModel):
    product_id: Optional[Union[int, str, UUID]] = Field(None, description="ID kandidat produk atau ID sementara")
    nama_produk: Optional[str] = Field(None, description="Nama kandidat produk")
    harga: Optional[float] = Field(None, description="Harga produk dalam Rupiah")
    ukuran: Optional[float] = Field(None, description="Ukuran/volume/berat produk")
    satuan: Optional[str] = Field(None, description="Satuan ukuran produk (ml, l, gr, kg, pcs, dll)")
    kategori: Optional[str] = Field(None, description="Kategori produk (opsional)")

class RecommendationRequest(BaseModel):
    category: Optional[str] = Field(None, description="Kategori filter/pembanding opsional")
    items: List[RecommendationCandidate] = Field(..., description="Daftar kandidat produk yang akan dievaluasi")

class RankedProductItem(BaseModel):
    product_id: Optional[str] = Field(None, description="ID kandidat produk")
    nama_produk: str = Field(..., description="Nama produk yang terverifikasi")
    harga: float = Field(..., description="Harga produk asli dalam Rupiah")
    ukuran_original: float = Field(..., description="Ukuran asli produk")
    satuan_original: str = Field(..., description="Satuan asli produk")
    normalized_ukuran: float = Field(..., description="Ukuran setelah dikonversi ke base unit")
    base_unit: str = Field(..., description="Satuan dasar (ml, g, atau pcs)")
    harga_per_unit: float = Field(..., description="Harga per unit (harga / normalized_ukuran)")
    unit_price_label: str = Field(..., description="Label harga per unit berformat (misal: 'Rp60,00 / ml')")
    rank: int = Field(..., description="Peringkat produk (1 = Best Value)")
    is_best_value: bool = Field(False, description="True jika produk merupakan Best Value")
    badge: Optional[str] = Field(None, description="Badge visual (misal: 'BEST VALUE')")
    explanation: str = Field(..., description="Alasan transparan mengapa produk memperoleh peringkat ini")

class ExcludedProductItem(BaseModel):
    product_id: Optional[str] = Field(None, description="ID kandidat produk jika ada")
    nama_produk: Optional[str] = Field(None, description="Nama produk yang dikecualikan")
    harga: Optional[float] = Field(None, description="Harga produk (jika ada)")
    ukuran: Optional[float] = Field(None, description="Ukuran produk (jika ada)")
    satuan: Optional[str] = Field(None, description="Satuan produk (jika ada)")
    reason: str = Field(..., description="Alasan produk tidak diikutsertakan dalam kalkulasi Best Value")

class RecommendationResponse(BaseModel):
    total_evaluated: int = Field(..., description="Total jumlah kandidat yang dievaluasi")
    total_valid: int = Field(..., description="Jumlah produk valid yang berhasil diperingkatkan")
    total_excluded: int = Field(..., description="Jumlah produk yang dikecualikan dari perhitungan")
    best_value: Optional[RankedProductItem] = Field(None, description="Produk dengan nilai ekonomi terbaik (Peringkat #1)")
    ranked_items: List[RankedProductItem] = Field(default_factory=list, description="Daftar produk valid terurut dari nilai terbaik")
    excluded_items: List[ExcludedProductItem] = Field(default_factory=list, description="Daftar produk yang dikecualikan dari perhitungan")

