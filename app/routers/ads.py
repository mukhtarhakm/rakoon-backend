import os
import math
import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status, UploadFile, File
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from app.database import get_db, supabase
from app.dependencies import get_current_user, get_current_admin_user
from app.models.db_models import Store, StoreOwner, AdCampaign, PaymentTransaction
from app.services import xendit_service
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
    status: str = "active"
    payment_method: Optional[str] = "QRIS"
    payment_ref: Optional[str] = None
    payment_status: Optional[str] = "unpaid"

    model_config = {"from_attributes": True, "extra": "ignore"}



class InitiatePaymentResponse(BaseModel):
    campaign_id: str
    external_id: str
    xendit_invoice_id: Optional[str] = None
    amount: int
    currency: str = "IDR"
    status: str
    invoice_url: Optional[str] = None
    expiry_date: Optional[str] = None


class PaymentStatusResponse(BaseModel):
    campaign_id: str
    campaign_status: str
    payment_status: str
    transaction_status: Optional[str] = None
    external_id: Optional[str] = None
    xendit_invoice_id: Optional[str] = None
    invoice_url: Optional[str] = None
    amount: Optional[int] = None
    currency: Optional[str] = "IDR"
    paid_at: Optional[str] = None
    expires_at: Optional[str] = None

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
    payment_ref: Optional[str] = Field(None, description="Diabaikan oleh server; referensi invoice resmi di-generate oleh server")

    model_config = {"extra": "ignore"}


class ClaimStoreRequest(BaseModel):
    store_id: str = Field(..., description="ID toko yang ingin diklaim/dikelola")


class VerifyStoreClaimRequest(BaseModel):
    user_id: str = Field(..., description="ID pengguna yang mengajukan klaim")
    store_id: str = Field(..., description="ID toko yang diklaim")


class PendingStoreClaimItem(BaseModel):
    user_id: str
    store_id: str
    store_nama: str
    created_at: str


class MyStoreResponse(BaseModel):
    is_claimed: bool
    claim_status: Optional[str] = None
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


def _parse_iso(val: Optional[str]) -> Optional[datetime]:
    if not val:
        return None
    try:
        s = val.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _has_verified_provider_proof(campaign: AdCampaign, db: Optional[Session] = None) -> bool:
    """
    Memeriksa apakah kampanye memiliki bukti pembayaran yang benar-benar terverifikasi
    dari payment gateway resmi (Xendit) yang tersimpan di PaymentTransaction berstatus 'PAID'.
    """
    if campaign.payment_status != "paid" or campaign.status != "active":
        return False

    if hasattr(campaign, "transactions") and campaign.transactions:
        return any(tx.status == "PAID" and tx.provider == "xendit" for tx in campaign.transactions)

    if db is not None:
        return (
            db.query(PaymentTransaction)
            .filter(
                PaymentTransaction.campaign_id == campaign.id,
                PaymentTransaction.status == "PAID",
                PaymentTransaction.provider == "xendit",
            )
            .first()
            is not None
        )

    return False


def _is_demo_mode_allowed() -> bool:
    """
    Mode demo HANYA diizinkan jika secara eksplisit diaktifkan melalui konfigurasi server
    (environment variable ALLOW_DEMO_ADS='true' dan bukan environment production).
    Parameter request publik (include_demo) TIDAK BISA mengaktifkan mode demo
    jika konfigurasi server tidak mengizinkannya atau pada environment production.
    """
    app_env = os.getenv("ENVIRONMENT", "development").lower()
    if app_env == "production":
        return False
    return os.getenv("ALLOW_DEMO_ADS", "false").lower() == "true"


def _is_demo_campaign(campaign: AdCampaign) -> bool:
    """
    Mengidentifikasi kampanye contoh/demo (misal seed awal sistem atau dummy testing).
    """
    owner_str = str(getattr(campaign, "owner_user_id", ""))
    return (
        owner_str == "00000000-0000-0000-0000-000000000000"
        or getattr(campaign, "payment_status", "") == "demo"
        or getattr(campaign, "payment_method", "") == "DEMO"
    )


@router.get("/home-banners", response_model=List[HomeBannerItem], status_code=status.HTTP_200_OK)
def get_home_banners(
    lat: float = Query(-7.7829, description="Latitude posisi pengguna"),
    lng: float = Query(110.4083, description="Longitude posisi pengguna"),
    radius_km: float = Query(15.0, description="Maksimum radius toko dari pengguna dalam km"),
    include_demo: bool = Query(False, description="Tampilkan banner contoh/demo secara eksplisit (terpisah dari iklan berbayar)"),
    db: Session = Depends(get_db)
):
    """
    Mengambil daftar banner iklan aktif dari toko terdekat di Yogyakarta.
    Hanya kampanye yang memiliki bukti pembayaran resmi dari payment provider (Xendit)
    yang ditayangkan sebagai iklan berbayar komersial.
    Data historis berstatus 'paid' tanpa bukti provider tidak akan ditayangkan sebagai iklan komersial.
    Banner demo hanya ditampilkan jika flag include_demo atau ALLOW_DEMO_ADS aktif, dan
    ditandai secara transparan sebagai payment_status='demo'.
    """
    now = datetime.now(timezone.utc)

    # Pastikan data demo terinisialisasi jika tabel masih kosong
    _ensure_seed_campaigns(db)

    # Ambil kampanye yang berstatus active
    all_active = (
        db.query(AdCampaign)
        .filter(
            AdCampaign.status == "active"
        )
        .all()
    )

    # Filter yang belum expired
    unexpired = [c for c in all_active if _ensure_utc(c.expires_at) > now]

    # Mode demo HANYA aktif jika diizinkan konfigurasi server non-production
    # DAN diminta secara eksplisit melalui parameter query include_demo=True
    is_demo_active = _is_demo_mode_allowed() and include_demo

    # Filter ketat: Hanya iklan terverifikasi bukti provider resmi (fail-closed) atau demo terpisah
    campaigns: List[AdCampaign] = []
    for c in unexpired:
        if _has_verified_provider_proof(c):
            campaigns.append(c)
        elif is_demo_active and _is_demo_campaign(c):
            campaigns.append(c)

    if not campaigns:
        return []

    # Ambil toko terkait
    store_ids = {c.store_id for c in campaigns}
    stores = db.query(Store).filter(Store.id.in_(store_ids)).all()
    store_map = {str(s.id): s for s in stores}
    verified_owners = {
        (str(owner.user_id), str(owner.store_id))
        for owner in db.query(StoreOwner).filter(
            StoreOwner.status == "verified",
            StoreOwner.verified_at.isnot(None),
        ).all()
    }

    jogja_center_lat, jogja_center_lng = -7.7829, 110.4083
    user_dist_to_jogja = haversine_distance(lat, lng, jogja_center_lat, jogja_center_lng)
    is_outside_jogja = user_dist_to_jogja > 50.0

    banner_items: List[HomeBannerItem] = []

    for c in campaigns:
        is_demo = _is_demo_campaign(c)
        # Jika bukan demo, pastikan pemilik toko terverifikasi
        if not is_demo and (str(c.owner_user_id), str(c.store_id)) not in verified_owners:
            continue
        store = store_map.get(str(c.store_id))
        if not store:
            continue

        store_lat = store.lat if store.lat is not None else jogja_center_lat
        store_lng = store.lng if store.lng is not None else jogja_center_lng

        if is_outside_jogja:
            dist = haversine_distance(jogja_center_lat, jogja_center_lng, store_lat, store_lng)
        else:
            dist = haversine_distance(lat, lng, store_lat, store_lng)

        exp_utc = _ensure_utc(c.expires_at)
        delta = exp_utc - now
        days_left = max(1, delta.days + (1 if delta.seconds > 0 else 0))

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
                status=getattr(c, "status", "active"),
                payment_method="DEMO" if is_demo else getattr(c, "payment_method", "QRIS"),
                payment_ref=getattr(c, "payment_ref", None),
                payment_status="demo" if is_demo else "paid"
            )
        )

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
                status=getattr(c, "status", "active"),
                payment_method=getattr(c, "payment_method", "QRIS"),
                payment_ref=getattr(c, "payment_ref", None),
                payment_status=getattr(c, "payment_status", "unpaid")
            )
        )

    return MyStoreResponse(
        is_claimed=owner.status == "verified" and owner.verified_at is not None,
        claim_status="verified" if owner.status == "verified" and owner.verified_at is not None else "pending",
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

    existing_owner = (
        db.query(StoreOwner)
        .filter(
            StoreOwner.store_id == store.id,
            StoreOwner.status == "verified",
            StoreOwner.verified_at.isnot(None),
        )
        .first()
    )
    if existing_owner and str(existing_owner.user_id) != user_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Toko ini sudah memiliki pemilik terverifikasi."
        )

    owner = db.query(StoreOwner).filter(StoreOwner.user_id == user_id).first()
    if not owner:
        owner = StoreOwner(
            user_id=user_id,
            store_id=store.id,
            status="pending"
        )
        db.add(owner)
    else:
        if owner.status == "verified" and owner.verified_at is not None:
            if str(owner.store_id) != str(store.id):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Akun ini sudah menjadi pemilik terverifikasi toko lain."
                )
            return get_my_store(user_id=user_id, db=db)
        owner.store_id = store.id
        owner.status = "pending"
        owner.verified_at = None

    db.commit()
    db.refresh(owner)

    return get_my_store(user_id=user_id, db=db)


@router.get("/pending-claims", response_model=List[PendingStoreClaimItem], status_code=status.HTTP_200_OK)
def get_pending_store_claims(
    admin_user: dict = Depends(get_current_admin_user),
    db: Session = Depends(get_db)
):
    """Daftar klaim yang harus diperiksa oleh admin."""
    rows = (
        db.query(StoreOwner, Store)
        .join(Store, StoreOwner.store_id == Store.id)
        .filter((StoreOwner.status != "verified") | (StoreOwner.verified_at.is_(None)))
        .order_by(StoreOwner.created_at.asc())
        .all()
    )
    return [
        PendingStoreClaimItem(
            user_id=str(owner.user_id),
            store_id=str(store.id),
            store_nama=store.nama,
            created_at=owner.created_at.isoformat(),
        )
        for owner, store in rows
    ]


@router.post("/verify-claim", response_model=MyStoreResponse, status_code=status.HTTP_200_OK)
def verify_store_claim(
    payload: VerifyStoreClaimRequest,
    admin_user: dict = Depends(get_current_admin_user),
    db: Session = Depends(get_db)
):
    """Setujui klaim toko setelah bukti kepemilikan diperiksa oleh admin."""
    owner = (
        db.query(StoreOwner)
        .filter(StoreOwner.user_id == payload.user_id, StoreOwner.store_id == payload.store_id)
        .first()
    )
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Klaim toko tidak ditemukan.")

    # Serialize approvals for the same store on PostgreSQL before checking ownership.
    db.query(Store).filter(Store.id == owner.store_id).with_for_update().first()
    other_owner = (
        db.query(StoreOwner)
        .filter(
            StoreOwner.store_id == owner.store_id,
            StoreOwner.status == "verified",
            StoreOwner.verified_at.isnot(None),
            StoreOwner.user_id != owner.user_id,
        )
        .first()
    )
    if other_owner:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Toko sudah memiliki pemilik terverifikasi.")

    owner.status = "verified"
    owner.verified_at = datetime.now(timezone.utc)
    db.commit()
    return get_my_store(user_id=payload.user_id, db=db)


@router.post("/upload-banner", status_code=status.HTTP_200_OK)
async def upload_ad_banner(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Mengunggah berkas foto flyer iklan promo ke Supabase Storage.
    """
    owner = (
        db.query(StoreOwner)
        .filter(
            StoreOwner.user_id == user_id,
            StoreOwner.status == "verified",
            StoreOwner.verified_at.isnot(None),
        )
        .first()
    )
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Hanya pemilik toko terverifikasi yang dapat mengunggah banner iklan."
        )

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

    owner = (
        db.query(StoreOwner)
        .filter(
            StoreOwner.user_id == user_id,
            StoreOwner.store_id == store.id,
            StoreOwner.status == "verified",
            StoreOwner.verified_at.isnot(None),
        )
        .first()
    )
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Hanya pemilik toko terverifikasi yang dapat membuat kampanye iklan."
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
    # Server-generated official invoice reference; client-supplied payment_ref is NEVER trusted as proof
    invoice_ref = f"INV-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"

    # New campaigns start strictly in pending_payment and unpaid state.
    # Active & paid status can only be granted by official payment provider verification.
    campaign = AdCampaign(
        store_id=store.id,
        owner_user_id=user_id,
        title=payload.title.strip(),
        banner_url=payload.banner_url.strip(),
        duration_days=payload.duration_days,
        price_paid=pkg["price"],
        payment_method=payload.payment_method or "QRIS",
        payment_ref=invoice_ref,
        payment_status="unpaid",
        status="pending_payment",
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
        status=campaign.status,
        payment_method=campaign.payment_method,
        payment_ref=campaign.payment_ref,
        payment_status=campaign.payment_status
    )


@router.get("/campaigns/{campaign_id}", response_model=HomeBannerItem, status_code=status.HTTP_200_OK)
def get_campaign_detail(
    campaign_id: str,
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Mengambil detail satu kampanye iklan. Hanya dapat diakses oleh pemilik toko yang memiliki kampanye tersebut.
    """
    campaign = db.query(AdCampaign).filter(AdCampaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Kampanye iklan tidak ditemukan."
        )

    if str(campaign.owner_user_id) != str(user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Anda tidak memiliki izin untuk mengakses kampanye toko ini."
        )

    store = db.query(Store).filter(Store.id == campaign.store_id).first()
    now = datetime.now(timezone.utc)
    exp_utc = _ensure_utc(campaign.expires_at)
    delta = exp_utc - now
    days_left = max(0, delta.days + (1 if delta.seconds > 0 else 0)) if exp_utc > now else 0

    return HomeBannerItem(
        id=str(campaign.id),
        store_id=str(campaign.store_id),
        store_nama=store.nama if store else "Toko",
        store_alamat=store.alamat if store else None,
        store_lat=store.lat if store else None,
        store_lng=store.lng if store else None,
        title=campaign.title,
        banner_url=campaign.banner_url,
        duration_days=campaign.duration_days,
        price_paid=campaign.price_paid,
        distance_km=0.0,
        expires_at=campaign.expires_at.isoformat(),
        days_left=days_left,
        status=campaign.status,
        payment_method=getattr(campaign, "payment_method", "QRIS"),
        payment_ref=getattr(campaign, "payment_ref", None),
        payment_status=getattr(campaign, "payment_status", "unpaid")
    )


@router.post("/campaigns/{campaign_id}/pay", response_model=InitiatePaymentResponse, status_code=status.HTTP_200_OK)
def initiate_campaign_payment(
    campaign_id: str,
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Memulai checkout pembayaran kampanye via payment gateway resmi Xendit Sandbox.
    Membuat invoice Xendit dan mengembalikan URL checkout resmi.
    """
    campaign = db.query(AdCampaign).filter(AdCampaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Kampanye iklan tidak ditemukan."
        )

    if str(campaign.owner_user_id) != str(user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Anda tidak memiliki izin untuk membayar kampanye toko ini."
        )

    owner = (
        db.query(StoreOwner)
        .filter(
            StoreOwner.user_id == user_id,
            StoreOwner.store_id == campaign.store_id,
            StoreOwner.status == "verified",
            StoreOwner.verified_at.isnot(None),
        )
        .first()
    )
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Hanya pemilik toko terverifikasi yang dapat membayar kampanye iklan."
        )

    if campaign.payment_status == "paid" or campaign.status == "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Kampanye iklan ini sudah lunas atau aktif."
        )

    now = datetime.now(timezone.utc)
    if _ensure_utc(campaign.expires_at) < now and campaign.status == "expired":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Kampanye iklan ini sudah kedaluwarsa."
        )

    try:
        config = xendit_service.get_xendit_config()
    except xendit_service.XenditConfigError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Payment gateway Xendit belum dikonfigurasi: {e}"
        )

    # Idempotensi: jika sudah ada transaksi PENDING dengan invoice_url yang valid, gunakan kembali
    existing_tx = (
        db.query(PaymentTransaction)
        .filter(
            PaymentTransaction.campaign_id == campaign.id,
            PaymentTransaction.status == "PENDING",
            PaymentTransaction.invoice_url.isnot(None),
        )
        .order_by(PaymentTransaction.created_at.desc())
        .first()
    )
    if existing_tx:
        return InitiatePaymentResponse(
            campaign_id=str(campaign.id),
            external_id=existing_tx.external_id,
            xendit_invoice_id=existing_tx.xendit_invoice_id,
            amount=int(existing_tx.amount),
            currency=existing_tx.currency,
            status=existing_tx.status,
            invoice_url=existing_tx.invoice_url,
            expiry_date=None,
        )

    external_id = campaign.payment_ref or f"INV-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"
    if not campaign.payment_ref:
        campaign.payment_ref = external_id
        db.commit()

    description = f"Kampanye Iklan Rakoon: {campaign.title} ({campaign.duration_days} Hari)"
    try:
        xendit_resp = xendit_service.create_invoice(
            external_id=external_id,
            amount=campaign.price_paid,
            description=description,
        )
    except xendit_service.XenditAPIError as e:
        logger.error(f"Gagal memanggil Xendit invoice API: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Gagal menghubungi gateway pembayaran Xendit. Silakan coba kembali."
        )

    tx = PaymentTransaction(
        campaign_id=campaign.id,
        owner_user_id=campaign.owner_user_id,
        provider="xendit",
        environment=config["environment"],
        external_id=external_id,
        xendit_invoice_id=xendit_resp.get("id"),
        amount=campaign.price_paid,
        currency="IDR",
        status="PENDING",
        invoice_url=xendit_resp.get("invoice_url"),
        created_at=now,
        updated_at=now,
    )
    db.add(tx)
    try:
        db.commit()
        db.refresh(tx)
    except Exception as e:
        db.rollback()
        logger.error(f"Database error saving payment transaction: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error saving payment transaction"
        )

    return InitiatePaymentResponse(
        campaign_id=str(campaign.id),
        external_id=tx.external_id,
        xendit_invoice_id=tx.xendit_invoice_id,
        amount=int(tx.amount),
        currency=tx.currency,
        status=tx.status,
        invoice_url=tx.invoice_url,
        expiry_date=xendit_resp.get("expiry_date"),
    )


@router.post("/webhook/xendit", status_code=status.HTTP_200_OK)
async def xendit_webhook(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Menerima notifikasi webhook resmi dari Xendit untuk pembaruan status transaksi.
    Hanya webhook dengan callback token yang sah dan transaksi yang cocok yang dapat memutasi status.
    """
    token = request.headers.get("x-callback-token")
    if not xendit_service.verify_webhook_token(token):
        logger.warning("Xendit webhook rejected: invalid or missing callback token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing callback token"
        )

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload"
        )

    xendit_id = payload.get("id")
    external_id = payload.get("external_id")
    event_status = (payload.get("status") or "").upper()
    paid_amount = payload.get("paid_amount") if payload.get("paid_amount") is not None else payload.get("amount")
    currency = payload.get("currency", "IDR")

    if not external_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing external_id in webhook payload"
        )

    tx = (
        db.query(PaymentTransaction)
        .filter(PaymentTransaction.external_id == external_id)
        .first()
    )
    if not tx:
        logger.warning(f"Webhook received for unknown external_id: {external_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found"
        )

    if currency and currency != tx.currency:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Currency mismatch"
        )

    if paid_amount is not None and abs(float(paid_amount) - float(tx.amount)) > 0.01:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Amount mismatch"
        )

    now = datetime.now(timezone.utc)
    if event_status in ("PAID", "SETTLED"):
        # Idempotensi: jika sudah PAID, kembalikan 200 tanpa mengulang aktivasi
        if tx.status == "PAID":
            return {"status": "already_processed", "message": "Transaction already verified and paid"}

        campaign = db.query(AdCampaign).filter(AdCampaign.id == tx.campaign_id).first()
        if not campaign:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Associated campaign not found"
            )

        tx.status = "PAID"
        tx.paid_at = _parse_iso(payload.get("paid_at")) or now
        tx.updated_at = now
        if xendit_id:
            tx.xendit_invoice_id = xendit_id

        campaign.payment_status = "paid"
        campaign.status = "active"
        campaign.payment_ref = xendit_id or tx.external_id
        campaign.start_at = now
        campaign.expires_at = now + timedelta(days=campaign.duration_days)

        try:
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"Database error committing webhook update: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Database error processing webhook"
            )

        logger.info(f"Campaign {campaign.id} successfully activated via Xendit webhook")
        return {"status": "success", "message": "Payment verified and campaign activated"}

    elif event_status == "EXPIRED":
        if tx.status != "PAID":
            tx.status = "EXPIRED"
            tx.updated_at = now
            try:
                db.commit()
            except Exception as e:
                db.rollback()
                logger.error(f"Database error committing webhook update: {e}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Database error processing webhook"
                )
        return {"status": "success", "message": "Transaction marked as EXPIRED"}

    elif event_status == "FAILED":
        if tx.status != "PAID":
            tx.status = "FAILED"
            tx.updated_at = now
            try:
                db.commit()
            except Exception as e:
                db.rollback()
                logger.error(f"Database error committing webhook update: {e}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Database error processing webhook"
                )
        return {"status": "success", "message": "Transaction marked as FAILED"}

    return {"status": "ignored", "message": f"Event status {event_status} ignored"}


@router.get("/campaigns/{campaign_id}/payment-status", response_model=PaymentStatusResponse, status_code=status.HTTP_200_OK)
def get_campaign_payment_status(
    campaign_id: str,
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Mengambil status pembayaran dan transaksi kampanye iklan terkini.
    Hanya dapat diakses oleh pemilik kampanye yang sah.
    """
    campaign = db.query(AdCampaign).filter(AdCampaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Kampanye iklan tidak ditemukan."
        )

    if str(campaign.owner_user_id) != str(user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Anda tidak memiliki izin untuk melihat status pembayaran kampanye ini."
        )

    tx = (
        db.query(PaymentTransaction)
        .filter(PaymentTransaction.campaign_id == campaign.id)
        .order_by(PaymentTransaction.created_at.desc())
        .first()
    )

    return PaymentStatusResponse(
        campaign_id=str(campaign.id),
        campaign_status=campaign.status,
        payment_status=campaign.payment_status,
        transaction_status=tx.status if tx else None,
        external_id=tx.external_id if tx else campaign.payment_ref,
        xendit_invoice_id=tx.xendit_invoice_id if tx else None,
        invoice_url=tx.invoice_url if tx else None,
        amount=int(tx.amount) if tx else campaign.price_paid,
        currency=tx.currency if tx else "IDR",
        paid_at=tx.paid_at.isoformat() if tx and tx.paid_at else None,
        expires_at=campaign.expires_at.isoformat() if campaign.expires_at else None,
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
            payment_status="demo",
            payment_method="DEMO",
            start_at=now,
            expires_at=expires
        )
        db.add(campaign)

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"Failed to auto-seed ad campaigns: {e}")
