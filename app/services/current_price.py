"""
Harga terkini: satu aturan untuk katalog, perbandingan toko, rekomendasi beranda, dan asisten anggaran.

Aturan (keputusan tim):
- Per (produk, toko) ambil entri TERBARU yang berstatus verified; pending/rejected tidak dihitung.
- Tidak ada jendela waktu yang menyembunyikan produk: harga lama tetap dikembalikan, ditandai stale
  bila umurnya lebih dari STALE_AFTER_DAYS.
- Entri dengan timestamp identik diputuskan deterministik lewat id terbesar.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, Optional, Tuple

from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.db_models import PriceEntry
from app.models.schemas import VerificationStatus

STALE_AFTER_DAYS = 90


@dataclass(frozen=True)
class CurrentPrice:
    product_id: str
    store_id: str
    harga: int
    timestamp: datetime  # UTC, timezone-aware
    stale: bool


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def get_current_prices(
    db: Session,
    product_ids: Optional[Iterable] = None,
    store_ids: Optional[Iterable] = None,
    now: Optional[datetime] = None,
) -> Dict[Tuple[str, str], CurrentPrice]:
    """
    Harga terkini per (product_id, store_id) sebagai string. product_ids/store_ids=None berarti semua;
    daftar kosong berarti tidak ada hasil.
    """
    verified = PriceEntry.status_verifikasi == VerificationStatus.VERIFIED.value
    filters = [verified]
    for column, values in ((PriceEntry.product_id, product_ids), (PriceEntry.store_id, store_ids)):
        if values is not None:
            values = [str(v) for v in values]
            if not values:
                return {}
            filters.append(column.in_(values))

    latest = (
        db.query(
            PriceEntry.product_id.label("pid"),
            PriceEntry.store_id.label("sid"),
            func.max(PriceEntry.timestamp).label("max_ts"),
        )
        .filter(*filters)
        .group_by(PriceEntry.product_id, PriceEntry.store_id)
        .subquery()
    )
    rows = (
        db.query(PriceEntry)
        .join(latest, and_(
            PriceEntry.product_id == latest.c.pid,
            PriceEntry.store_id == latest.c.sid,
            PriceEntry.timestamp == latest.c.max_ts,
        ))
        .filter(verified)
        .all()
    )

    now = now or datetime.now(timezone.utc)
    chosen: Dict[Tuple[str, str], PriceEntry] = {}
    for row in rows:
        key = (str(row.product_id), str(row.store_id))
        if key not in chosen or str(row.id) > str(chosen[key].id):
            chosen[key] = row

    return {
        key: CurrentPrice(
            product_id=key[0],
            store_id=key[1],
            harga=int(row.harga),
            timestamp=_as_utc(row.timestamp),
            stale=(now - _as_utc(row.timestamp)).days > STALE_AFTER_DAYS,  # "lebih dari 90 hari" (hari penuh)
        )
        for key, row in chosen.items()
    }


def pick_cheapest(prices: Iterable[CurrentPrice]) -> Optional[CurrentPrice]:
    """Termurah; harga segar didahulukan atas harga stale yang lebih murah. Seri diputuskan oleh store_id."""
    prices = list(prices)
    pool = [p for p in prices if not p.stale] or prices
    return min(pool, key=lambda p: (p.harga, p.store_id), default=None)
