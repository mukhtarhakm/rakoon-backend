from pydantic import BaseModel, Field, BeforeValidator
from datetime import datetime
from typing import Optional, Union, List, Annotated, Any
from uuid import UUID
from enum import Enum

class VerificationStatus(str, Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class ProductCategory(str, Enum):
    MAKANAN_POKOK = "Makanan Pokok"
    MAKANAN_INSTAN = "Makanan Instan"
    CAMILAN = "Camilan"
    MINUMAN = "Minuman"
    SUSU_OLAHAN = "Susu & Olahan"
    BUMBU_SAUS = "Bumbu & Saus"
    PERAWATAN_DIRI = "Perawatan Diri"
    PRODUK_RUMAH_TANGGA = "Produk Rumah Tangga"
    KESEHATAN = "Kesehatan"
    BAYI = "Bayi"
    LAINNYA = "Lainnya"

def validate_category(v: Any) -> ProductCategory:
    if isinstance(v, ProductCategory):
        return v
    if isinstance(v, str):
        cleaned = v.strip()
        # Exact or case-insensitive match against Enum values
        for member in ProductCategory:
            if member.value.lower() == cleaned.lower():
                return member
        # Case-insensitive match against Enum names
        for member in ProductCategory:
            if member.name.lower() == cleaned.replace(" ", "_").replace("&", "").replace("__", "_").lower():
                return member
    return ProductCategory.LAINNYA

ProductCategoryType = Annotated[ProductCategory, BeforeValidator(validate_category)]

class PriceEntryCreate(BaseModel):
    product_id: Union[int, str, UUID] = Field(..., description="ID of the product")
    store_id: Union[int, str, UUID] = Field(..., description="ID of the store")
    harga: int = Field(..., description="Price value of the product at the store")
    sumber_user_id: Union[int, str, UUID] = Field(..., description="ID of the user submitting the price")

class PriceEntryOut(BaseModel):
    id: Union[int, str, UUID] = Field(..., description="ID of the price entry")
    product_id: Union[int, str, UUID] = Field(..., description="ID of the product")
    store_id: Union[int, str, UUID] = Field(..., description="ID of the store")
    harga: int = Field(..., description="Price value of the product at the store")
    timestamp: datetime = Field(..., description="Timestamp of when the price entry was created")
    status_verifikasi: str = Field(..., description="Verification status of the price entry (e.g., 'pending')")

    model_config = {"from_attributes": True}

class ScanResultItem(BaseModel):
    nama_produk: Optional[str] = Field(None, description="Nama produk yang terdeteksi")
    harga: Optional[float] = Field(None, description="Harga produk (angka, null jika tidak terbaca)")
    ukuran: Optional[float] = Field(None, description="Ukuran produk (angka, null jika tidak terbaca)")
    satuan: Optional[str] = Field(None, description="Satuan ukuran produk (null jika tidak terbaca)")
    kategori: ProductCategoryType = Field(default=ProductCategory.LAINNYA, description="Kategori produk")
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
    kategori: ProductCategoryType = Field(default=ProductCategory.LAINNYA, description="Kategori produk hasil konfirmasi/koreksi")

class ConfirmRequest(BaseModel):
    store_id: Union[int, str, UUID] = Field(..., description="ID dari toko tempat scan dilakukan")
    user_id: Union[int, str, UUID] = Field(..., description="ID dari pengguna yang melakukan konfirmasi")
    items: List[ConfirmItem] = Field(..., description="Daftar item hasil scan yang dikonfirmasi")

class ConfirmResponse(BaseModel):
    items_saved: int = Field(..., description="Jumlah entri harga yang berhasil disimpan")
    products_created: int = Field(..., description="Jumlah produk baru yang berhasil dibuat")
    message: str = Field(..., description="Pesan status konfirmasi")
    scan_session_id: Optional[str] = Field(None, description="ID sesi scan yang dibuat")

class RecentScanItem(BaseModel):
    id: str = Field(..., description="ID scan session")
    store_id: str = Field(..., description="ID toko")
    store_name: Optional[str] = Field(None, description="Nama toko")
    timestamp: datetime = Field(..., description="Waktu sesi scan")
    product_count: int = Field(..., description="Jumlah produk yang dipindai dalam sesi")

    model_config = {"from_attributes": True}


class ScanSessionProductItem(BaseModel):
    product_id: str = Field(..., description="ID produk")
    nama_produk: str = Field(..., description="Nama produk")
    kategori: str = Field(..., description="Kategori produk")
    ukuran: Optional[float] = Field(None, description="Ukuran produk")
    satuan: Optional[str] = Field(None, description="Satuan ukuran")
    harga: int = Field(..., description="Harga produk dalam sesi ini")

    model_config = {"from_attributes": True}


class ScanSessionDetailResponse(BaseModel):
    id: str = Field(..., description="ID scan session")
    store_id: str = Field(..., description="ID toko")
    store_name: Optional[str] = Field(None, description="Nama toko")
    timestamp: datetime = Field(..., description="Waktu sesi scan")
    product_count: int = Field(..., description="Jumlah total produk dalam sesi")
    items: List[ScanSessionProductItem] = Field(default_factory=list, description="Daftar produk dalam sesi scan")

    model_config = {"from_attributes": True}



# ---------------------------------------------------------------------------
# F3 — Price History Schemas
# ---------------------------------------------------------------------------

class PriceHistoryItem(BaseModel):
    """Satu baris riwayat harga dari tabel price_entries."""

    id: Union[UUID, int, str] = Field(..., description="ID entri harga")
    product_id: Union[UUID, int, str] = Field(..., description="ID produk")
    store_id: Union[UUID, str] = Field(..., description="ID toko tempat harga dicatat")
    store_name: Optional[str] = Field(None, description="Nama toko tempat harga dicatat")
    harga: int = Field(..., description="Harga produk (satuan: Rupiah)")
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
    product_name: str = Field(..., description="Nama produk yang diminta")
    total: int = Field(..., description="Jumlah total entri yang dikembalikan")
    items: List[PriceHistoryItem] = Field(default_factory=list, description="Daftar entri riwayat harga")
    trend: List[PriceTrendPoint] = Field(default_factory=list, description="Daftar titik data grafik tren harga")


# ---------------------------------------------------------------------------
# F2 — Price Compare Schemas
# ---------------------------------------------------------------------------

class PriceCompareItem(BaseModel):
    store_id: str = Field(..., description="ID toko")
    nama_toko: str = Field(..., description="Nama toko")
    lat: float = Field(..., description="Latitude toko")
    lng: float = Field(..., description="Longitude toko")
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

class DimensionRecommendationGroup(BaseModel):
    dimension: str = Field(..., description="Dimensi satuan ('volume', 'weight', atau 'count')")
    dimension_label: str = Field(..., description="Label dimensi berformat (misal: 'Volume (ml)', 'Berat (g)')")
    base_unit: str = Field(..., description="Satuan dasar dimensi ('ml', 'g', atau 'pcs')")
    is_comparable: bool = Field(True, description="True jika terdapat minimal 2 produk terbandingkan")
    message: Optional[str] = Field(None, description="Pesan tambahan jika produk tidak dapat dibandingkan (misal item tunggal)")
    best_value: Optional[RankedProductItem] = Field(None, description="Produk Best Value (Peringkat #1) dalam kelompok dimensi ini")
    ranked_items: List[RankedProductItem] = Field(default_factory=list, description="Daftar produk terurut dalam kelompok dimensi ini")

class CategoryRecommendationGroup(BaseModel):
    kategori: str = Field(..., description="Nama kategori produk")
    dimension_groups: List[DimensionRecommendationGroup] = Field(default_factory=list, description="Daftar kelompok dimensi dalam kategori ini")

class RecommendationResponse(BaseModel):
    total_evaluated: int = Field(..., description="Total jumlah kandidat yang dievaluasi")
    total_valid: int = Field(..., description="Jumlah produk valid yang berhasil diperingkatkan")
    total_excluded: int = Field(..., description="Jumlah produk yang dikecualikan dari perhitungan")
    categories: List[CategoryRecommendationGroup] = Field(default_factory=list, description="Daftar kelompok rekomendasi per kategori")
    excluded_items: List[ExcludedProductItem] = Field(default_factory=list, description="Daftar produk yang dikecualikan dari perhitungan")


# ==============================================================================
# FEATURE: SMART BUDGET SHOPPING ASSISTANT SCHEMAS
# ==============================================================================

class BudgetItemInput(BaseModel):
    product_id: Union[int, str, UUID] = Field(..., description="ID produk yang ingin dibeli")
    qty: int = Field(..., gt=0, description="Kuantitas/jumlah produk yang dibeli (harus > 0)")

class BudgetRecommendRequest(BaseModel):
    budget: float = Field(..., gt=0, description="Total budget yang dialokasikan pengguna dalam Rupiah")
    items: List[BudgetItemInput] = Field(..., min_length=1, description="Daftar barang dan kuantitas yang dibutuhkan")

class BudgetItemResult(BaseModel):
    product_id: str = Field(..., description="ID produk")
    nama_produk: str = Field(..., description="Nama produk")
    qty: int = Field(..., description="Jumlah item yang dibeli")
    harga_satuan: float = Field(..., description="Harga satuan produk di toko terpilih")
    subtotal: float = Field(..., description="Subtotal harga (harga_satuan * qty)")

class StoreInfoOutput(BaseModel):
    store_id: str = Field(..., description="ID dari toko")
    nama: str = Field(..., description="Nama toko")
    alamat: Optional[str] = Field(None, description="Alamat toko (bisa null)")
    lat: Optional[float] = Field(None, description="Latitude lokasi toko")
    lng: Optional[float] = Field(None, description="Longitude lokasi toko")

class ProductAvailability(BaseModel):
    product_id: str = Field(..., description="ID produk")
    nama_produk: str = Field(..., description="Nama produk")
    is_available: bool = Field(..., description="True jika produk tersedia di setidaknya satu toko")
    harga_terendah: Optional[float] = Field(None, description="Harga terendah produk di toko yang menyediakannya")
    toko_terendah: Optional[str] = Field(None, description="Nama toko yang memiliki harga terendah tersebut")

class AlternativeStoreOutput(BaseModel):
    store_info: StoreInfoOutput
    total_cost: float = Field(..., description="Total biaya belanja di toko alternatif")
    remaining_budget: float = Field(..., description="Sisa budget di toko alternatif")
    is_full_match: bool = Field(..., description="True jika toko memiliki 100% barang")
    matched_products_count: int = Field(..., description="Jumlah barang yang cocok")
    items: List[BudgetItemResult] = Field(default_factory=list, description="Rincian item belanja di toko alternatif")

class BudgetRecommendResponse(BaseModel):
    budget: float = Field(..., description="Total budget pengguna")
    total_cost: float = Field(..., description="Total biaya belanja di toko rekomendasi")
    remaining_budget: float = Field(..., description="Sisa budget (budget - total_cost)")
    is_full_match: bool = Field(False, description="True jika ditemukan toko yang memiliki 100% seluruh barang")
    recommended_store: Optional[StoreInfoOutput] = Field(None, description="Toko yang direkomendasikan (null jika tidak ada Full Match / budget kurang)")
    items: List[BudgetItemResult] = Field(default_factory=list, description="Rincian item belanja di toko rekomendasi")
    explanation: str = Field(..., description="Penjelasan rinci hasil rekomendasi budget shopping")
    product_availabilities: Optional[List[ProductAvailability]] = Field(None, description="Rincian ketersediaan produk jika tidak ada full match")
    store_alternatives: Optional[List[AlternativeStoreOutput]] = Field(None, description="Toko alternatif selain rekomendasi utama")


