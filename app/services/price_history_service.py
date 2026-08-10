"""
F3 — Price History Query Service
=================================
Fungsi-fungsi ini mengambil data histori harga dari tabel `price_entries`
yang sudah ada. Tidak ada perubahan skema; semua query bersifat read-only
terhadap data yang sudah diinsert oleh endpoint F1 (scan/confirm).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.db_models import PriceEntry, Product
from app.models.schemas import PriceHistoryItem, PriceHistoryResponse, PriceTrendPoint


class DateRange(str, Enum):
    """Enum rentang tanggal yang bisa dipilih user (FR-3.3)."""

    ONE_MONTH = "1m"
    THREE_MONTHS = "3m"
    SIX_MONTHS = "6m"
    ALL = "all"


def _resolve_date_window(
    range_enum: Optional[DateRange],
    start_date: Optional[datetime],
    end_date: Optional[datetime],
) -> tuple[Optional[datetime], Optional[datetime]]:
    """
    Resolusi rentang tanggal.

    Prioritas:
    1. start_date / end_date eksplisit (jika keduanya disediakan).
    2. range_enum (1m / 3m / 6m / all).
    3. Tidak ada filter → kembalikan (None, None) → semua data.
    """
    if start_date and end_date:
        return start_date, end_date

    if range_enum and range_enum != DateRange.ALL:
        now = datetime.utcnow()
        delta_map: dict[DateRange, timedelta] = {
            DateRange.ONE_MONTH: timedelta(days=30),
            DateRange.THREE_MONTHS: timedelta(days=90),
            DateRange.SIX_MONTHS: timedelta(days=180),
        }
        delta = delta_map.get(range_enum)
        if delta:
            return now - delta, now

    return None, None


def calculate_price_trend(items: List[PriceHistoryItem]) -> List[PriceTrendPoint]:
    """
    Transformasi data riwayat harga mentah menjadi daftar titik data tren (chart-ready).

    Aturan Agregasi Multi-Entri per Hari (Docstring FR-3.4 / Task B3):
    ------------------------------------------------------------------
    1. Pengelompokan (Grouping):
       Data dikelompokkan berdasarkan pasangan (tanggal, store_id) di mana tanggal
       diformat sebagai `YYYY-MM-DD` dari field `recorded_at`.
    2. Agregasi Harga (Multi-entry Strategy):
       Jika terdapat beberapa entri harga untuk toko yang sama pada hari yang sama
       (misal dari scan beberapa user), harga dihitung berdasarkan RATA-RATA (mean)
       dari seluruh entri tersebut, kemudian dibulatkan ke integer terdekat.
    3. Urutan (Ordering):
       Hasil akhir diurutkan secara ascending berdasarkan tanggal (`YYYY-MM-DD`)
       dan kemudian `store_id`.

    Parameters
    ----------
    items: List[PriceHistoryItem]
        Daftar entri riwayat harga yang sudah di-filter.

    Returns
    -------
    List[PriceTrendPoint]
        Daftar titik data grafik tren harga ({date, store_id, price}).
    """
    if not items:
        return []

    grouped: dict[tuple[str, str], list[int]] = {}
    for item in items:
        date_str = item.recorded_at.strftime("%Y-%m-%d")
        key = (date_str, item.store_id)
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(item.harga)

    trend_points: List[PriceTrendPoint] = []
    sorted_keys = sorted(grouped.keys(), key=lambda k: (k[0], k[1]))

    for (date_str, store_id), prices in sorted_keys:
        avg_price = int(round(sum(prices) / len(prices)))
        trend_points.append(
            PriceTrendPoint(
                date=date_str,
                store_id=store_id,
                price=avg_price,
            )
        )

    return trend_points


def get_price_history(
    db: Session,
    product_id: int,
    *,
    store_id: Optional[str] = None,
    range_enum: Optional[DateRange] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> PriceHistoryResponse:
    """
    Ambil riwayat harga untuk satu produk dari tabel `price_entries`.

    Parameters
    ----------
    db:
        SQLAlchemy session (injected via Depends(get_db)).
    product_id:
        ID produk yang dicari (integer, sesuai kolom products.id).
    store_id:
        Filter opsional berdasarkan store_id (FR-3.3).
    range_enum:
        Rentang waktu preset: "1m", "3m", "6m", atau "all" (FR-3.3).
        Diabaikan jika start_date dan end_date keduanya disediakan.
    start_date:
        Batas awal rentang tanggal (opsional, inklusif).
    end_date:
        Batas akhir rentang tanggal (opsional, inklusif).

    Returns
    -------
    PriceHistoryResponse
        Wrapper berisi product_id, total jumlah entri, daftar PriceHistoryItem,
        serta data tren harga (PriceTrendPoint) untuk grafik.

    Raises
    ------
    ValueError
        Jika produk dengan product_id tidak ditemukan di tabel products.
    """
    # 1. Validasi keberadaan produk
    product_exists = db.query(Product.id).filter(Product.id == product_id).first()
    if not product_exists:
        raise ValueError(f"Produk dengan ID {product_id} tidak ditemukan.")

    # 2. Bangun query dasar
    query = db.query(PriceEntry).filter(PriceEntry.product_id == product_id)

    # 3. Filter store_id (FR-3.3)
    if store_id:
        query = query.filter(PriceEntry.store_id == store_id)

    # 4. Filter rentang tanggal (FR-3.3)
    resolved_start, resolved_end = _resolve_date_window(range_enum, start_date, end_date)
    if resolved_start:
        query = query.filter(PriceEntry.timestamp >= resolved_start)
    if resolved_end:
        query = query.filter(PriceEntry.timestamp <= resolved_end)

    # 5. Urutkan ascending (timestamp lama → baru) agar grafik tren konsisten
    records: List[PriceEntry] = query.order_by(PriceEntry.timestamp.asc()).all()

    # 6. Serialisasi ke Pydantic (alias timestamp → recorded_at)
    items = [PriceHistoryItem.model_validate(r) for r in records]

    # 7. Hitung data tren harga untuk grafik (Task B3)
    trend = calculate_price_trend(items)

    return PriceHistoryResponse(
        product_id=product_id,
        total=len(items),
        items=items,
        trend=trend,
    )

