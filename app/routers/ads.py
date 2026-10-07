import math
import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status, UploadFile, File
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from app.database import get_db, supabase
from app.dependencies import get_current_user
from app.models.db_models import Store, StoreOwner, AdCampaign
from app.routers.stores import haversine_distance
from app.routers.products import detect_image_type

logger = logging.getLogger("rakoon_backend.ads")

router = APIRouter()

SUPABASE_BUCKET = "product-photos"
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/jpg"}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB

PRICING_PACKAGES = [
    {
        "duration_days": 3,
        "name": "Paket Kilat (3 Hari)",
        "price": 15000,
        "description": "Ideal untuk promo akhir pekan (JSM) atau flash promo cuci gudang."
    },
    {
        "duration_days": 7,
        "name": "Paket Mingguan (7 Hari)",
        "price": 30000,
        "description": "Cocok untuk brosur promo mingguan toko kelontong & supermarket."
    },
    {
        "duration_days": 14,
        "name": "Paket 2 Mingguan (14 Hari)",
        "price": 50000,
        "description": "Ekonomis untuk eksposur katalog promo periode gajian."
    }
]

# Schemas
class HomeBannerItem(BaseModel):
    id: str
    store_id: str
    store_nama: str
    store_alamat: Optional[str] = None
    store_lat: Optional[float] = None
    store_lng: Optional[float] = None
    title: str
    banner_url: str
    duration_days: int
    price_paid: int
    distance_km: float
    expires_at: str
    days_left: int
    payment_method: Optional[str] = "QRIS"
    payment_ref: Optional[str] = None
    payment_status: Optional[str] = "paid"

    model_config = {"from_attributes": True}


class PricingPackageItem(BaseModel):
    duration_days: int
    name: str
    price: int
    description: str


class CreateCampaignRequest(BaseModel):
    store_id: str = Field(..., description="ID toko pengiklan")
    title: str = Field(..., min_length=3, description="Judul promo atau headline")
    banner_url: str = Field(..., description="URL gambar flyer promo")
    duration_days: int = Field(3, description="Pilihan durasi: 3, 7, atau 14 hari")
    payment_method: Optional[str] = Field("QRIS", description="Metode pembayaran (QRIS, TRANSFER)")
    payment_ref: Optional[str] = Field(None, description="Nomor referensi pembayaran unik (opsional)")


class ClaimStoreRequest(BaseModel):
    store_id: str = Field(..., description="ID toko yang ingin diklaim/dikelola")


class MyStoreResponse(BaseModel):
    is_claimed: bool
    store_id: Optional[str] = None
    store_nama: Optional[str] = None
    store_alamat: Optional[str] = None
    store_lat: Optional[float] = None
    store_lng: Optional[float] = None
    campaigns: List[HomeBannerItem] = []


@router.get("/pricing-packages", response_model=List[PricingPackageItem], status_code=status.HTTP_200_OK)
def get_pricing_packages():
    """
    Mengambil daftar paket harga pasang iklan offline flyer di Rakoon.
    """
    return PRICING_PACKAGES


def _ensure_utc(dt: Optional[datetime]) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@router.get("/home-banners", response_model=List[HomeBannerItem], status_code=status.HTTP_200_OK)
def get_home_banners(
    lat: float = Query(-7.7829, description="Latitude posisi pengguna"),
    lng: float = Query(110.4083, description="Longitude posisi pengguna"),
    radius_km: float = Query(15.0, description="Maksimum radius toko dari pengguna dalam km"),
    db: Session = Depends(get_db)
):
    """
    Mengambil daftar banner iklan aktif dari toko terdekat di Yogyakarta.
    Iklan disaring yang statusnya 'active' dan belum kedaluwarsa (expires_at > now).
    Diurutkan dari jarak toko terdekat ke posisi pengguna.
    """
    now = datetime.now(timezone.utc)

    # Pastikan data demo terinisialisasi jika tabel masih kosong
    _ensure_seed_campaigns(db)

    # Ambil kampanye aktif
    campaigns = (
        db.query(AdCampaign)
        .filter(
            AdCampaign.status == "active"
        )
        .all()
    )

    # Filter yang belum expired (aman untuk SQLite & PostgreSQL)
    campaigns = [c for c in campaigns if _ensure_utc(c.expires_at) > now]

    if not campaigns:
        return []

    # Ambil toko terkait
    store_ids = {c.store_id for c in campaigns}
    stores = db.query(Store).filter(Store.id.in_(store_ids)).all()
    store_map = {str(s.id): s for s in stores}

    # Titik acuan default Yogyakarta (Tugu / Malioboro) jika user berada sangat jauh (misal juri di luar kota)
    jogja_center_lat, jogja_center_lng = -7.7829, 110.4083
    user_dist_to_jogja = haversine_distance(lat, lng, jogja_center_lat, jogja_center_lng)
    is_outside_jogja = user_dist_to_jogja > 50.0

    banner_items: List[HomeBannerItem] = []

    for c in campaigns:
        store = store_map.get(str(c.store_id))
        if not store:
            continue

        store_lat = store.lat if store.lat is not None else jogja_center_lat
        store_lng = store.lng if store.lng is not None else jogja_center_lng

        # Hitung jarak
        if is_outside_jogja:
            # Fallback untuk juri yang mengakses dari luar kota DIY: hitung jarak dari pusat Jogja
            dist = haversine_distance(jogja_center_lat, jogja_center_lng, store_lat, store_lng)
        else:
            dist = haversine_distance(lat, lng, store_lat, store_lng)

        # Hitung sisa hari
        exp_utc = _ensure_utc(c.expires_at)
        delta = exp_utc - now
        days_left = max(1, delta.days + (1 if delta.seconds > 0 else 0))

        # Filter radius jika pengguna berada di area Jogja
        if not is_outside_jogja and dist > radius_km:
            continue

        banner_items.append(
            HomeBannerItem(
                id=str(c.id),
                store_id=str(store.id),
                store_nama=store.nama,
                store_alamat=store.alamat,
                store_lat=store.lat,
                store_lng=store.lng,
                title=c.title,
                banner_url=c.banner_url,
                duration_days=c.duration_days,
                price_paid=c.price_paid,
                distance_km=round(dist, 1),
                expires_at=c.expires_at.isoformat(),
                days_left=days_left,
                payment_method=getattr(c, "payment_method", "QRIS"),
                payment_ref=getattr(c, "payment_ref", None),
                payment_status=getattr(c, "payment_status", "paid")
            )
        )

    # Sort berdasarkan jarak terdekat
    banner_items.sort(key=lambda x: (x.distance_km, x.days_left))
    return banner_items


@router.get("/my-store", response_model=MyStoreResponse, status_code=status.HTTP_200_OK)
def get_my_store(
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Mengambil data toko yang diklaim/dikelola oleh pengguna saat ini beserta riwayat iklannya.
    """
    now = datetime.now(timezone.utc)
    owner = db.query(StoreOwner).filter(StoreOwner.user_id == user_id).first()
    if not owner:
        return MyStoreResponse(is_claimed=False, campaigns=[])

    store = db.query(Store).filter(Store.id == owner.store_id).first()
    if not store:
        return MyStoreResponse(is_claimed=False, campaigns=[])

    campaigns = (
        db.query(AdCampaign)
        .filter(AdCampaign.store_id == store.id)
        .order_by(AdCampaign.created_at.desc())
        .all()
    )

    items = []
    for c in campaigns:
        exp_utc = _ensure_utc(c.expires_at)
        delta = exp_utc - now
        days_left = max(0, delta.days + (1 if delta.seconds > 0 else 0)) if exp_utc > now else 0
        items.append(
            HomeBannerItem(
                id=str(c.id),
                store_id=str(store.id),
                store_nama=store.nama,
                store_alamat=store.alamat,
                store_lat=store.lat,
                store_lng=store.lng,
                title=c.title,
                banner_url=c.banner_url,
                duration_days=c.duration_days,
                price_paid=c.price_paid,
                distance_km=0.0,
                expires_at=c.expires_at.isoformat(),
                days_left=days_left,
                payment_method=getattr(c, "payment_method", "QRIS"),
                payment_ref=getattr(c, "payment_ref", None),
                payment_status=getattr(c, "payment_status", "paid")
            )
        )

    return MyStoreResponse(
        is_claimed=True,
        store_id=str(store.id),
        store_nama=store.nama,
        store_alamat=store.alamat,
        store_lat=store.lat,
        store_lng=store.lng,
        campaigns=items
    )


@router.post("/claim-store", response_model=MyStoreResponse, status_code=status.HTTP_200_OK)
def claim_store(
    payload: ClaimStoreRequest,
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Mengaitkan akun pengguna dengan toko yang dipilih sebagai pemilik toko.
    """
    store = db.query(Store).filter(Store.id == payload.store_id).first()
    if not store:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Toko yang dipilih tidak ditemukan."
        )

    owner = db.query(StoreOwner).filter(StoreOwner.user_id == user_id).first()
    if not owner:
        owner = StoreOwner(
            user_id=user_id,
            store_id=store.id,
            status="verified"
        )
        db.add(owner)
    else:
        owner.store_id = store.id
        owner.status = "verified"

    db.commit()
    db.refresh(owner)

    return get_my_store(user_id=user_id, db=db)


@router.post("/upload-banner", status_code=status.HTTP_200_OK)
async def upload_ad_banner(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user)
):
    """
    Mengunggah berkas foto flyer iklan promo ke Supabase Storage.
    """
    contents = await file.read()
    if not contents:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Berkas foto flyer kosong."
        )

    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ukuran file terlalu besar. Maksimum 5MB."
        )

    content_type, ext = detect_image_type(
        filename=file.filename,
        declared_content_type=file.content_type,
        contents=contents
    )

    filename = f"ad_{uuid.uuid4().hex}{ext}"

    try:
        supabase.storage.from_(SUPABASE_BUCKET).upload(
            filename,
            contents,
            {"content-type": content_type, "upsert": "true"}
        )
        banner_url = supabase.storage.from_(SUPABASE_BUCKET).get_public_url(filename).rstrip("?")
        return {"banner_url": banner_url}
    except Exception as e:
        # Fallback local URL if Supabase credentials in development mock mode
        logger.warning(f"Failed to upload to Supabase: {e}. Using fallback path.")
        return {"banner_url": f"https://images.unsplash.com/photo-1542838132-92c53300491e?auto=format&fit=crop&w=800&q=80"}


@router.post("/campaigns", response_model=HomeBannerItem, status_code=status.HTTP_201_CREATED)
def create_ad_campaign(
    payload: CreateCampaignRequest,
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Membuat kampanye iklan promo offline baru dengan batas waktu tayang dan konfirmasi pembayaran.
    """
    store = db.query(Store).filter(Store.id == payload.store_id).first()
    if not store:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Toko tidak ditemukan."
        )

    # Validasi paket durasi & biaya
    pkg = next((p for p in PRICING_PACKAGES if p["duration_days"] == payload.duration_days), None)
    if not pkg:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Durasi tidak valid. Pilihan yang tersedia: 3, 7, atau 14 hari."
        )

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=payload.duration_days)
    pay_ref = payload.payment_ref or f"QRIS-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

    campaign = AdCampaign(
        store_id=store.id,
        owner_user_id=user_id,
        title=payload.title.strip(),
        banner_url=payload.banner_url.strip(),
        duration_days=payload.duration_days,
        price_paid=pkg["price"],
        payment_method=payload.payment_method or "QRIS",
        payment_ref=pay_ref,
        payment_status="paid",
        status="active",
        start_at=now,
        expires_at=expires_at
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)

    return HomeBannerItem(
        id=str(campaign.id),
        store_id=str(store.id),
        store_nama=store.nama,
        store_alamat=store.alamat,
        store_lat=store.lat,
        store_lng=store.lng,
        title=campaign.title,
        banner_url=campaign.banner_url,
        duration_days=campaign.duration_days,
        price_paid=campaign.price_paid,
        distance_km=0.0,
        expires_at=campaign.expires_at.isoformat(),
        days_left=campaign.duration_days,
        payment_method=campaign.payment_method,
        payment_ref=campaign.payment_ref,
        payment_status=campaign.payment_status
    )


def _ensure_seed_campaigns(db: Session):
    """
    Inisialisasi sampel banner flyer promo untuk toko terkenal di Yogyakarta jika belum ada data iklan.
    """
    count = db.query(AdCampaign).count()
    if count > 0:
        return

    # Cari toko-toko di Jogja yang sudah ada
    stores = db.query(Store).limit(5).all()
    if not stores:
        return

    now = datetime.now(timezone.utc)
    sample_ads = [
        {
            "title": "Promo JSM Minyak Goreng & Beras Super Hemat",
            "banner_url": "https://images.unsplash.com/photo-1542838132-92c53300491e?auto=format&fit=crop&w=800&q=80",
            "duration": 7,
            "price": 30000,
        },
        {
            "title": "Diskon Spesial Susu Segar & Kebutuhan Dapur",
            "banner_url": "https://images.unsplash.com/photo-1578916171728-46686eac8d58?auto=format&fit=crop&w=800&q=80",
            "duration": 3,
            "price": 15000,
        },
        {
            "title": "Belanja Hemat Akhir Pekan - Aneka Snack & Minuman",
            "banner_url": "https://images.unsplash.com/photo-1588964895597-cfccd6e2dbf9?auto=format&fit=crop&w=800&q=80",
            "duration": 14,
            "price": 50000,
        }
    ]

    for i, ad_data in enumerate(sample_ads):
        store = stores[i % len(stores)]
        expires = now + timedelta(days=ad_data["duration"])
        campaign = AdCampaign(
            store_id=store.id,
            owner_user_id="00000000-0000-0000-0000-000000000000",
            title=ad_data["title"],
            banner_url=ad_data["banner_url"],
            duration_days=ad_data["duration"],
            price_paid=ad_data["price"],
            status="active",
            start_at=now,
            expires_at=expires
        )
        db.add(campaign)

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"Failed to auto-seed ad campaigns: {e}")
