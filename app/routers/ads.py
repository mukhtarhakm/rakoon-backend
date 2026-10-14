import os
import math
import uuid
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status, UploadFile, File
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field

from app.database import get_db
from app.dependencies import get_current_user, get_current_admin_user, get_current_user_profile
from app.models.db_models import Store, StoreOwner, AdCampaign, PaymentTransaction, User, StoreSubmission
from app.services import xendit_service
from app.routers.stores import haversine_distance
from app.services.storage_upload import read_upload, detect_content_type, upload_image

logger = logging.getLogger("rakoon_backend.ads")

router = APIRouter()

PRIVATE_DOCS_DIR = Path(__file__).resolve().parent.parent.parent / "private_storage" / "documents"
PRIVATE_DOCS_DIR.mkdir(parents=True, exist_ok=True)

def _has_store_submissions_table(db: Session) -> bool:
    try:
        from sqlalchemy import inspect
        insp = inspect(db.get_bind())
        return insp.has_table("store_submissions")
    except Exception:
        return False


def _get_user_latest_submission(user_id: str, db: Session) -> Optional[StoreSubmission]:
    if not _has_store_submissions_table(db):
        return None
    try:
        return (
            db.query(StoreSubmission)
            .filter(StoreSubmission.user_id == user_id)
            .order_by(StoreSubmission.created_at.desc())
            .first()
        )
    except Exception as e:
        logger.warning(f"Error querying StoreSubmission table: {e}")
        return None


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
    applicant_name: Optional[str] = Field(None, description="Nama lengkap pemohon")
    applicant_phone: Optional[str] = Field(None, description="Nomor kontak yang dapat diverifikasi")
    applicant_role: Optional[str] = Field(None, description="Peran pemohon (Pemilik, Pengurus Perusahaan, Manajer Cabang)")
    document_url: Optional[str] = Field(None, description="URL/Path dokumen legalitas usaha (NIB)")
    store_photo_url: Optional[str] = Field(None, description="URL/Path foto tampak depan toko")

    model_config = {"extra": "ignore"}


class SubmitNewStoreRequest(BaseModel):
    store_name: str = Field(..., min_length=2, description="Nama toko")
    business_entity_name: Optional[str] = Field(None, description="Nama badan usaha (opsional)")
    store_address: str = Field(..., min_length=5, description="Alamat lengkap toko")
    store_category: Optional[str] = Field(None, description="Jenis atau kategori toko (opsional)")
    applicant_name: str = Field(..., min_length=2, description="Nama lengkap pemohon")
    applicant_phone: str = Field(..., min_length=8, description="Nomor kontak pemohon")
    applicant_role: str = Field(..., min_length=2, description="Peran pemohon")
    document_url: Optional[str] = Field(None, description="URL dokumen legalitas usaha (NIB)")
    store_photo_url: Optional[str] = Field(None, description="URL foto tampak depan toko")

    model_config = {"extra": "ignore"}


class VerifyStoreClaimRequest(BaseModel):
    user_id: str = Field(..., description="ID pengguna yang mengajukan klaim")
    store_id: str = Field(..., description="ID toko yang diklaim")


class RejectStoreClaimRequest(BaseModel):
    user_id: str = Field(..., description="ID pengguna yang mengajukan klaim")
    store_id: str = Field(..., description="ID toko yang diklaim")
    reason: Optional[str] = Field(None, description="Alasan penolakan klaim opsional")


class VerifyNewStoreRequest(BaseModel):
    submission_id: str = Field(..., description="ID pengajuan toko baru")


class RejectNewStoreRequest(BaseModel):
    submission_id: str = Field(..., description="ID pengajuan toko baru")
    reason: Optional[str] = Field(None, description="Alasan penolakan pengajuan")


class UserSubmissionItem(BaseModel):
    id: str
    submission_type: str  # "claim" atau "new_store"
    store_id: Optional[str] = None
    store_name: str
    store_address: Optional[str] = None
    store_category: Optional[str] = None
    business_entity_name: Optional[str] = None
    applicant_name: Optional[str] = None
    applicant_phone: Optional[str] = None
    applicant_role: Optional[str] = None
    document_url: Optional[str] = None
    store_photo_url: Optional[str] = None
    status: str  # "pending", "under_review", "verified", "rejected"
    rejection_reason: Optional[str] = None
    created_at: str


class PendingStoreClaimItem(BaseModel):
    user_id: str
    store_id: Optional[str] = None
    store_nama: str
    store_alamat: Optional[str] = None
    user_nama: Optional[str] = None
    user_email: Optional[str] = None
    status: str = "pending"
    created_at: str
    has_verified_owner: bool = False
    submission_id: Optional[str] = None
    claim_type: str = "claim"  # "claim" atau "new_store"
    applicant_name: Optional[str] = None
    applicant_phone: Optional[str] = None
    applicant_role: Optional[str] = None
    business_entity_name: Optional[str] = None
    store_category: Optional[str] = None
    document_url: Optional[str] = None
    store_photo_url: Optional[str] = None


class MyStoreResponse(BaseModel):
    is_claimed: bool
    claim_status: Optional[str] = None
    store_id: Optional[str] = None
    store_nama: Optional[str] = None
    store_alamat: Optional[str] = None
    store_lat: Optional[float] = None
    store_lng: Optional[float] = None
    rejection_reason: Optional[str] = None
    campaigns: List[HomeBannerItem] = []
    applicant_name: Optional[str] = None
    applicant_role: Optional[str] = None
    document_url: Optional[str] = None
    store_photo_url: Optional[str] = None


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


def _reconcile_transaction_with_xendit(
    tx: PaymentTransaction,
    campaign: AdCampaign,
    db: Session,
) -> bool:
    """
    Safely reconciles a pending transaction with Xendit API.
    Acts as a fallback when webhooks are delayed, dropped, or unreachable (e.g. local environment).
    Never marks paid unless Xendit API explicitly confirms status as PAID or SETTLED.
    """
    if not tx or tx.status != "PENDING":
        return False

    try:
        x_inv = None
        if tx.xendit_invoice_id:
            x_inv = xendit_service.get_invoice_by_id(tx.xendit_invoice_id)
        if not x_inv and tx.external_id:
            x_inv = xendit_service.get_invoice_by_external_id(tx.external_id)

        if not x_inv or not isinstance(x_inv, dict):
            return False

        x_status = (x_inv.get("status") or "").upper()
        now = datetime.now(timezone.utc)

        if x_status in ("PAID", "SETTLED"):
            if tx.status != "PAID":
                tx.status = "PAID"
                tx.paid_at = _parse_iso(x_inv.get("paid_at")) or now
                tx.updated_at = now
                if x_inv.get("id"):
                    tx.xendit_invoice_id = x_inv["id"]

                campaign.payment_status = "paid"
                campaign.status = "active"
                campaign.payment_ref = x_inv.get("id") or tx.external_id
                campaign.start_at = now
                campaign.expires_at = now + timedelta(days=campaign.duration_days)

                db.commit()
                logger.info(f"Transaction {tx.external_id} for campaign {campaign.id} reconciled as PAID via Xendit API")
                return True
        elif x_status == "EXPIRED":
            if tx.status != "PAID":
                tx.status = "EXPIRED"
                tx.updated_at = now
                db.commit()
        elif x_status == "FAILED":
            if tx.status != "PAID":
                tx.status = "FAILED"
                tx.updated_at = now
                db.commit()
    except Exception as e:
        logger.warning(f"Error during Xendit payment reconciliation for {tx.external_id}: {e}")
        db.rollback()

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
        (str(user_id), str(store_id))
        for user_id, store_id in db.query(StoreOwner.user_id, StoreOwner.store_id).filter(
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
        # Cek apakah pengguna memiliki pengajuan toko baru (Jalur B) di DB yang sedang diproses atau ditolak
        sub = _get_user_latest_submission(user_id, db)
        if sub and sub.status != "verified":
            return MyStoreResponse(
                is_claimed=False,
                claim_status=sub.status,
                store_nama=sub.store_name,
                store_alamat=sub.store_address,
                rejection_reason=sub.rejection_reason if sub.status == "rejected" else None,
                applicant_name=getattr(sub, "applicant_name", None),
                applicant_role=getattr(sub, "applicant_role", None),
                document_url=getattr(sub, "document_url", None),
                store_photo_url=getattr(sub, "store_photo_url", None),
                campaigns=[]
            )
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

    # Reconcile pending campaigns with Xendit API as fallback for missed/delayed webhooks
    if xendit_service.is_xendit_sandbox_configured():
        for c in campaigns:
            if c.status == "pending_payment" or c.payment_status == "unpaid":
                tx = (
                    db.query(PaymentTransaction)
                    .filter(PaymentTransaction.campaign_id == c.id)
                    .order_by(PaymentTransaction.created_at.desc())
                    .first()
                )
                if tx and tx.status == "PENDING":
                    _reconcile_transaction_with_xendit(tx, c, db)

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
        claim_status="rejected" if owner.status == "rejected" else ("verified" if owner.status == "verified" and owner.verified_at is not None else "pending"),
        store_id=str(store.id),
        store_nama=store.nama,
        store_alamat=store.alamat,
        store_lat=store.lat,
        store_lng=store.lng,
        rejection_reason=owner.rejection_reason if owner.status == "rejected" else None,
        applicant_name=getattr(owner, "applicant_name", None),
        applicant_role=getattr(owner, "applicant_role", None),
        document_url=getattr(owner, "document_url", None),
        store_photo_url=getattr(owner, "store_photo_url", None),
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
    try:
        store = db.query(Store).filter(Store.id == payload.store_id).with_for_update().first()
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

        owner = db.query(StoreOwner).filter(StoreOwner.user_id == user_id).with_for_update().first()
        app_name = payload.applicant_name.strip() if payload.applicant_name and payload.applicant_name.strip() else None
        app_phone = payload.applicant_phone.strip() if payload.applicant_phone and payload.applicant_phone.strip() else None
        app_role = payload.applicant_role.strip() if payload.applicant_role and payload.applicant_role.strip() else None

        if not owner:
            owner = StoreOwner(
                user_id=user_id,
                store_id=store.id,
                status="pending",
                rejection_reason=None,
                applicant_name=app_name,
                applicant_phone=app_phone,
                applicant_role=app_role,
                document_url=payload.document_url,
                store_photo_url=payload.store_photo_url,
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
            owner.rejection_reason = None
            owner.applicant_name = app_name
            owner.applicant_phone = app_phone
            owner.applicant_role = app_role
            owner.document_url = payload.document_url
            owner.store_photo_url = payload.store_photo_url

        db.commit()
        db.refresh(owner)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Toko ini sudah memiliki pemilik terverifikasi atau klaim sedang diproses."
        )
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error in claim_store: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Terjadi kesalahan pada server saat memproses klaim toko."
        )

    return get_my_store(user_id=user_id, db=db)


@router.get("/pending-claims", response_model=List[PendingStoreClaimItem], status_code=status.HTTP_200_OK)
def get_pending_store_claims(
    admin_user: dict = Depends(get_current_admin_user),
    db: Session = Depends(get_db)
):
    """Daftar klaim toko terdaftar dan pengajuan toko baru yang harus diperiksa oleh admin."""
    # 1. Klaim toko terdaftar (StoreOwner)
    rows = (
        db.query(StoreOwner, Store)
        .join(Store, StoreOwner.store_id == Store.id)
        .filter(StoreOwner.status != "rejected", (StoreOwner.status != "verified") | (StoreOwner.verified_at.is_(None)))
        .order_by(StoreOwner.created_at.asc())
        .all()
    )

    # 2. Pengajuan toko baru (StoreSubmission)
    sub_rows = []
    if _has_store_submissions_table(db):
        try:
            sub_rows = (
                db.query(StoreSubmission)
                .filter(StoreSubmission.status == "pending")
                .order_by(StoreSubmission.created_at.asc())
                .all()
            )
        except Exception as e:
            logger.warning(f"Error reading StoreSubmission table: {e}")

    user_ids = [str(owner.user_id) for owner, _ in rows] + [str(sub.user_id) for sub in sub_rows]
    store_ids = [store.id for _, store in rows]
    users_map = {str(u.id): u for u in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}

    verified_store_ids = set()
    if store_ids:
        verified_owners = (
            db.query(StoreOwner.store_id)
            .filter(
                StoreOwner.store_id.in_(store_ids),
                StoreOwner.status == "verified",
                StoreOwner.verified_at.isnot(None),
            )
            .all()
        )
        verified_store_ids = {str(so[0]) for so in verified_owners}

    results: List[PendingStoreClaimItem] = []

    # Map existing store claims
    for owner, store in rows:
        u = users_map.get(str(owner.user_id))
        results.append(
            PendingStoreClaimItem(
                user_id=str(owner.user_id),
                store_id=str(store.id),
                store_nama=store.nama,
                store_alamat=store.alamat,
                user_nama=u.nama if u else None,
                user_email=u.email if u else None,
                status=owner.status,
                created_at=owner.created_at.isoformat() if owner.created_at else "",
                has_verified_owner=str(store.id) in verified_store_ids,
                submission_id=str(owner.id),
                claim_type="claim",
                applicant_name=getattr(owner, "applicant_name", None),
                applicant_phone=getattr(owner, "applicant_phone", None),
                applicant_role=getattr(owner, "applicant_role", None),
                document_url=getattr(owner, "document_url", None),
                store_photo_url=getattr(owner, "store_photo_url", None),
            )
        )

    # Map new store submissions
    for sub in sub_rows:
        u = users_map.get(str(sub.user_id))
        results.append(
            PendingStoreClaimItem(
                user_id=str(sub.user_id),
                store_id=None,
                store_nama=sub.store_name,
                store_alamat=sub.store_address,
                user_nama=u.nama if u else None,
                user_email=u.email if u else None,
                status=sub.status,
                created_at=sub.created_at.isoformat() if sub.created_at else "",
                has_verified_owner=False,
                submission_id=str(sub.id),
                claim_type="new_store",
                applicant_name=getattr(sub, "applicant_name", None),
                applicant_phone=getattr(sub, "applicant_phone", None),
                applicant_role=getattr(sub, "applicant_role", None),
                business_entity_name=getattr(sub, "business_entity_name", None),
                store_category=getattr(sub, "store_category", None),
                document_url=getattr(sub, "document_url", None),
                store_photo_url=getattr(sub, "store_photo_url", None),
            )
        )

    results.sort(key=lambda x: x.created_at)
    return results


@router.post("/reject-claim", response_model=MyStoreResponse, status_code=status.HTTP_200_OK)
def reject_store_claim(
    payload: RejectStoreClaimRequest,
    admin_user: dict = Depends(get_current_admin_user),
    db: Session = Depends(get_db)
):
    """Tolak klaim toko oleh admin."""
    # ponytail: sets status to rejected without audit table; add audit log table when SLA tracking is needed
    try:
        # 1. Acquire row lock on Store first to serialize with verify/claim operations
        store = db.query(Store).filter(Store.id == payload.store_id).with_for_update().first()
        if not store:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Toko yang dipilih tidak ditemukan.")

        # 2. Acquire row lock on target StoreOwner inside transaction (avoids stale read)
        owner = (
            db.query(StoreOwner)
            .filter(StoreOwner.user_id == payload.user_id, StoreOwner.store_id == payload.store_id)
            .with_for_update()
            .first()
        )
        if not owner:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Klaim toko tidak ditemukan.")

        if owner.status == "verified" and owner.verified_at is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Klaim toko yang sudah terverifikasi tidak dapat ditolak."
            )

        if owner.status == "rejected":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Klaim toko sudah dalam status ditolak."
            )

        clean_reason = payload.reason.strip() if payload.reason and payload.reason.strip() else None

        owner.status = "rejected"
        owner.verified_at = None
        owner.rejection_reason = clean_reason
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise

    return get_my_store(user_id=payload.user_id, db=db)


@router.post("/verify-claim", response_model=MyStoreResponse, status_code=status.HTTP_200_OK)
def verify_store_claim(
    payload: VerifyStoreClaimRequest,
    admin_user: dict = Depends(get_current_admin_user),
    db: Session = Depends(get_db)
):
    """Setujui klaim toko setelah bukti kepemilikan diperiksa oleh admin."""
    try:
        # 1. Acquire row lock on Store first to serialize all claim decisions for this store
        store = db.query(Store).filter(Store.id == payload.store_id).with_for_update().first()
        if not store:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Toko yang dipilih tidak ditemukan.")

        # 2. Acquire row lock on target StoreOwner inside transaction (avoids stale read)
        owner = (
            db.query(StoreOwner)
            .filter(StoreOwner.user_id == payload.user_id, StoreOwner.store_id == payload.store_id)
            .with_for_update()
            .first()
        )
        if not owner:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Klaim toko tidak ditemukan.")

        if owner.status == "rejected":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Klaim toko yang sudah ditolak tidak dapat langsung disetujui. Pengguna harus mengajukan klaim ulang terlebih dahulu."
            )

        if owner.status == "verified" and owner.verified_at is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Klaim toko sudah dalam status terverifikasi."
            )

        # 3. Check if another user is already verified for this store
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
        owner.rejection_reason = None

        # 4. Atomically auto-reject competing pending claims for the same store
        competing_claims = (
            db.query(StoreOwner)
            .filter(
                StoreOwner.store_id == owner.store_id,
                StoreOwner.id != owner.id,
                StoreOwner.status == "pending",
            )
            .with_for_update()
            .all()
        )
        for competing in competing_claims:
            competing.status = "rejected"
            competing.verified_at = None
            competing.rejection_reason = "Toko telah diverifikasi untuk mitra lain."

        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Toko sudah memiliki pemilik terverifikasi."
        )
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise

    return get_my_store(user_id=payload.user_id, db=db)


@router.post("/submit-new-store", response_model=UserSubmissionItem, status_code=status.HTTP_201_CREATED)
def submit_new_store(
    payload: SubmitNewStoreRequest,
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Mengajukan pendaftaran toko baru yang belum tersedia di katalog (Jalur B: Pengajuan Toko Baru).
    Toko baru disimpan dalam antrean pending dan TIDAK langsung aktif di katalog publik.
    """
    clean_name = payload.store_name.strip()
    clean_address = payload.store_address.strip()
    if not clean_name or len(clean_name) < 2:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nama toko harus diisi minimal 2 karakter.")
    if not clean_address or len(clean_address) < 5:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Alamat toko harus diisi minimal 5 karakter.")

    # 1. Cek ketersediaan tabel store_submissions di database
    if not _has_store_submissions_table(db):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Fitur pengajuan toko baru belum tersedia karena skema database belum diperbarui. Silakan hubungi administrator."
        )

    # 2. Cek duplikasi di katalog toko publik
    existing_store = (
        db.query(Store)
        .filter(
            func.lower(Store.nama) == clean_name.lower(),
            func.lower(Store.alamat) == clean_address.lower()
        )
        .first()
    )
    if existing_store:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Toko dengan nama dan alamat serupa sudah terdaftar di katalog. Silakan gunakan jalur 'Klaim Toko Terdaftar' untuk mengklaim toko ini."
        )

    # 3. Cek pengajuan pending duplikat oleh user yang sama di database
    existing_sub = (
        db.query(StoreSubmission)
        .filter(
            StoreSubmission.user_id == user_id,
            StoreSubmission.status == "pending",
            func.lower(StoreSubmission.store_name) == clean_name.lower()
        )
        .first()
    )
    if existing_sub:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Anda sudah memiliki pengajuan aktif untuk toko ini yang sedang menunggu pemeriksaan admin."
        )

    # 4. Cek apakah user sudah memiliki toko terverifikasi
    verified_owner = (
        db.query(StoreOwner)
        .filter(
            StoreOwner.user_id == user_id,
            StoreOwner.status == "verified",
            StoreOwner.verified_at.isnot(None)
        )
        .first()
    )
    if verified_owner:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Akun ini sudah memiliki toko terverifikasi. Satu akun hanya dapat mengelola satu toko mitra."
        )

    sub_id = str(uuid.uuid4())
    cat = payload.store_category.strip() if payload.store_category and payload.store_category.strip() else None

    sub = StoreSubmission(
        id=sub_id,
        user_id=user_id,
        store_name=clean_name,
        business_entity_name=payload.business_entity_name.strip() if payload.business_entity_name else None,
        store_address=clean_address,
        store_category=cat or "Supermarket",
        applicant_name=payload.applicant_name.strip(),
        applicant_phone=payload.applicant_phone.strip(),
        applicant_role=payload.applicant_role.strip(),
        document_url=payload.document_url,
        store_photo_url=payload.store_photo_url,
        status="pending",
    )
    db.add(sub)
    try:
        db.commit()
        db.refresh(sub)
    except Exception as e:
        db.rollback()
        logger.error(f"Error saving store submission: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Gagal menyimpan pengajuan toko.")

    return UserSubmissionItem(
        id=sub_id,
        submission_type="new_store",
        store_id=None,
        store_name=clean_name,
        store_address=clean_address,
        store_category=cat,
        business_entity_name=payload.business_entity_name.strip() if payload.business_entity_name else None,
        applicant_name=payload.applicant_name.strip(),
        applicant_phone=payload.applicant_phone.strip(),
        applicant_role=payload.applicant_role.strip(),
        document_url=payload.document_url,
        store_photo_url=payload.store_photo_url,
        status="pending",
        rejection_reason=None,
        created_at=sub.created_at.isoformat() if sub.created_at else "",
    )


@router.get("/my-submissions", response_model=List[UserSubmissionItem], status_code=status.HTTP_200_OK)
def get_my_submissions(
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Mengambil riwayat seluruh pengajuan klaim dan pendaftaran toko milik pengguna."""
    items: List[UserSubmissionItem] = []

    # 1. Existing store claims (StoreOwner) dari database
    owners = (
        db.query(StoreOwner, Store)
        .join(Store, StoreOwner.store_id == Store.id)
        .filter(StoreOwner.user_id == user_id)
        .all()
    )
    for owner, store in owners:
        items.append(
            UserSubmissionItem(
                id=str(owner.id),
                submission_type="claim",
                store_id=str(store.id),
                store_name=store.nama,
                store_address=store.alamat,
                applicant_name=getattr(owner, "applicant_name", None),
                applicant_phone=getattr(owner, "applicant_phone", None),
                applicant_role=getattr(owner, "applicant_role", None),
                document_url=getattr(owner, "document_url", None),
                store_photo_url=getattr(owner, "store_photo_url", None),
                status=owner.status,
                rejection_reason=owner.rejection_reason if owner.status == "rejected" else None,
                created_at=owner.created_at.isoformat() if owner.created_at else "",
            )
        )

    # 2. New store submissions (StoreSubmission) dari database jika tabel tersedia
    if _has_store_submissions_table(db):
        try:
            subs = (
                db.query(StoreSubmission)
                .filter(StoreSubmission.user_id == user_id)
                .all()
            )
            for sub in subs:
                items.append(
                    UserSubmissionItem(
                        id=str(sub.id),
                        submission_type="new_store",
                        store_id=str(sub.created_store_id) if sub.created_store_id else None,
                        store_name=sub.store_name,
                        store_address=sub.store_address,
                        store_category=sub.store_category,
                        business_entity_name=sub.business_entity_name,
                        applicant_name=sub.applicant_name,
                        applicant_phone=sub.applicant_phone,
                        applicant_role=sub.applicant_role,
                        document_url=sub.document_url,
                        store_photo_url=sub.store_photo_url,
                        status=sub.status,
                        rejection_reason=sub.rejection_reason if sub.status == "rejected" else None,
                        created_at=sub.created_at.isoformat() if sub.created_at else "",
                    )
                )
        except Exception as e:
            logger.warning(f"Error querying StoreSubmission table: {e}")

    items.sort(key=lambda x: x.created_at, reverse=True)
    return items


@router.post("/verify-new-store", status_code=status.HTTP_200_OK)
def verify_new_store_submission(
    payload: VerifyNewStoreRequest,
    admin_user: dict = Depends(get_current_admin_user),
    db: Session = Depends(get_db)
):
    """Setujui pengajuan toko baru, aktifkan toko ke katalog publik, dan berikan akses pemilik terverifikasi."""
    if not _has_store_submissions_table(db):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Fitur verifikasi toko baru belum tersedia karena skema database belum diperbarui."
        )
    try:
        sub = db.query(StoreSubmission).filter(StoreSubmission.id == payload.submission_id).with_for_update().first()
        if not sub:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pengajuan toko baru tidak ditemukan.")

        if sub.status == "verified":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pengajuan toko baru sudah dalam status terverifikasi.")
        if sub.status == "rejected":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pengajuan toko yang sudah ditolak tidak dapat langsung disetujui.")

        clean_name = sub.store_name.strip()
        clean_address = sub.store_address.strip()

        # 1. Cek apakah toko dengan nama dan alamat serupa sudah dibuat selama pengajuan berlangsung
        store = (
            db.query(Store)
            .filter(
                func.lower(Store.nama) == clean_name.lower(),
                func.lower(Store.alamat) == clean_address.lower()
            )
            .with_for_update()
            .first()
        )
        if not store:
            store = Store(
                id=str(uuid.uuid4()),
                nama=clean_name,
                alamat=clean_address,
                lat=-7.7829,
                lng=110.4083,
            )
            db.add(store)
            db.flush()

        now = datetime.now(timezone.utc)
        # 2. Cek apakah user sudah memiliki owner record
        owner = db.query(StoreOwner).filter(StoreOwner.user_id == sub.user_id).with_for_update().first()
        if not owner:
            owner = StoreOwner(
                id=str(uuid.uuid4()),
                user_id=sub.user_id,
                store_id=store.id,
                status="verified",
                verified_at=now,
                rejection_reason=None
            )
            db.add(owner)
        else:
            owner.store_id = store.id
            owner.status = "verified"
            owner.verified_at = now
            owner.rejection_reason = None

        # 3. Auto-reject competing pending submissions for same store
        competing = (
            db.query(StoreSubmission)
            .filter(
                StoreSubmission.id != payload.submission_id,
                StoreSubmission.status == "pending",
                func.lower(StoreSubmission.store_name) == clean_name.lower()
            )
            .with_for_update()
            .all()
        )
        for c in competing:
            c.status = "rejected"
            c.rejection_reason = "Toko telah disetujui dan diverifikasi untuk mitra lain."
            c.reviewed_at = now
            c.reviewed_by = admin_user.get("user_id")

        sub.status = "verified"
        sub.created_store_id = store.id
        sub.reviewed_at = now
        sub.reviewed_by = admin_user.get("user_id")

        db.commit()
        return {
            "status": "success",
            "message": f"Toko '{store.nama}' berhasil diverifikasi dan ditambahkan ke katalog.",
            "store_id": str(store.id),
            "store_nama": store.nama
        }
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Toko sudah memiliki pemilik terverifikasi.")
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error in verify_new_store_submission: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Terjadi kesalahan pada server saat menyetujui toko baru.")


@router.post("/reject-new-store", status_code=status.HTTP_200_OK)
def reject_new_store_submission(
    payload: RejectNewStoreRequest,
    admin_user: dict = Depends(get_current_admin_user),
    db: Session = Depends(get_db)
):
    """Tolak pengajuan toko baru oleh admin dengan alasan penolakan."""
    if not _has_store_submissions_table(db):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Fitur penolakan toko baru belum tersedia karena skema database belum diperbarui."
        )
    try:
        clean_reason = payload.reason.strip() if payload.reason and payload.reason.strip() else None
        now = datetime.now(timezone.utc)
        sub = db.query(StoreSubmission).filter(StoreSubmission.id == payload.submission_id).with_for_update().first()
        if not sub:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pengajuan toko baru tidak ditemukan.")

        if sub.status == "verified":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pengajuan toko yang sudah terverifikasi tidak dapat ditolak.")
        if sub.status == "rejected":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pengajuan toko sudah dalam status ditolak.")

        sub.status = "rejected"
        sub.rejection_reason = clean_reason
        sub.reviewed_at = now
        sub.reviewed_by = admin_user.get("user_id")
        db.commit()

        return {"status": "success", "message": "Pengajuan toko baru telah ditolak.", "rejection_reason": clean_reason}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error in reject_new_store_submission: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Terjadi kesalahan pada server saat menolak pengajuan toko.")


@router.post("/upload-claim-document", status_code=status.HTTP_200_OK)
async def upload_claim_document(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user),
):
    """
    Mengunggah dokumen legalitas usaha (NIB, dsb) secara aman ke private storage.
    Hanya menerima berkas PDF atau gambar scan (JPEG, PNG, WebP) maks 5MB; tipe dicek dari isi berkas.
    Lokasi simpan (private_storage) dan awalan nama doc_{user_id}_ dipertahankan sampai gelombang d.
    """
    contents = await read_upload(file)
    content_type, ext = detect_content_type(contents, allow_pdf=True)

    filename = f"doc_{user_id}_{uuid.uuid4().hex[:16]}{ext}"
    target_path = PRIVATE_DOCS_DIR / filename
    with open(target_path, "wb") as f:
        f.write(contents)

    return {"document_url": f"/ads/documents/{filename}", "filename": filename}


@router.post("/upload-claim-photo", status_code=status.HTTP_200_OK)
async def upload_claim_photo(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user),
):
    """
    Mengunggah foto tampak depan toko yang memperlihatkan papan nama toko.
    Storage gagal -> 502/503 (tidak ada URL pengganti).
    """
    return {"photo_url": await upload_image(file, prefix="claim_photo_")}


@router.get("/documents/{filename}")
def get_private_document(
    filename: str,
    user_id: str = Depends(get_current_user),
    profile: dict = Depends(get_current_user_profile),
    db: Session = Depends(get_db)
):
    """
    Mengunduh dokumen legalitas dari private storage secara aman.
    Akses hanya diizinkan untuk admin atau pemohon pemilik dokumen tersebut.
    """
    safe_filename = Path(filename).name
    if safe_filename != filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nama berkas tidak valid.")

    file_path = PRIVATE_DOCS_DIR / safe_filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dokumen tidak ditemukan.")

    # Cek hak akses: Admin selalu diizinkan
    is_admin = profile.get("role") == "admin"
    if not is_admin:
        allowed = False
        # 1. Dokumen yang diunggah oleh user ini sendiri
        if safe_filename.startswith(f"doc_{user_id}_"):
            allowed = True

        # 2. Cek klaim toko user jika kolom document_url tersedia
        if not allowed:
            user_owners = db.query(StoreOwner).filter(StoreOwner.user_id == user_id).all()
            for o in user_owners:
                if safe_filename in (getattr(o, "document_url", None) or ""):
                    allowed = True
                    break

        # 3. Cek pengajuan toko baru user dari DB
        if not allowed and _has_store_submissions_table(db):
            try:
                sub = (
                    db.query(StoreSubmission)
                    .filter(
                        StoreSubmission.user_id == user_id,
                        StoreSubmission.document_url.like(f"%{safe_filename}%")
                    )
                    .first()
                )
                if sub:
                    allowed = True
            except Exception:
                pass

        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Akses ditolak: Anda tidak memiliki izin untuk melihat dokumen ini."
            )

    media_type = "application/pdf" if safe_filename.endswith(".pdf") else "image/jpeg"
    if safe_filename.endswith(".png"):
        media_type = "image/png"
    elif safe_filename.endswith(".webp"):
        media_type = "image/webp"

    return FileResponse(
        path=file_path,
        media_type=media_type,
        headers={"Cache-Control": "private, no-store"}
    )


@router.post("/upload-banner", status_code=status.HTTP_200_OK)
async def upload_ad_banner(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Mengunggah berkas foto flyer iklan promo ke Supabase Storage. Storage gagal -> 502/503.
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

    banner_url = await upload_image(file, prefix="ad_")
    return {"banner_url": banner_url}


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
    campaign = db.query(AdCampaign).filter(AdCampaign.id == campaign_id).with_for_update().first()
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
    campaign = db.query(AdCampaign).filter(AdCampaign.id == campaign_id).with_for_update().first()
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
    except IntegrityError:
        db.rollback()
        logger.info(f"Concurrent checkout race condition detected for {external_id}, reconciling...")
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
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Transaksi sedang diproses oleh permintaan lain. Silakan periksa status pembayaran."
        )
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
        .with_for_update()
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

        campaign = db.query(AdCampaign).filter(AdCampaign.id == tx.campaign_id).with_for_update().first()
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
    campaign = db.query(AdCampaign).filter(AdCampaign.id == campaign_id).with_for_update().first()
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

    # Reconcile pending transaction with Xendit API as fallback for missed/delayed webhooks
    if tx and tx.status == "PENDING" and xendit_service.is_xendit_sandbox_configured():
        _reconcile_transaction_with_xendit(tx, campaign, db)

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
    Inisialisasi sampel banner flyer promo untuk toko terkenal di Yogyakarta jika belum ada data iklan demo di development.
    """
    # ponytail: demo seeds only execute in non-production when ALLOW_DEMO_ADS=true
    if not _is_demo_mode_allowed():
        return

    # Cari toko-toko di Jogja yang sudah ada
    stores = db.query(Store).limit(5).all()
    if not stores:
        default_store = Store(
            id=str(uuid.uuid4()),
            nama="Pamela 6 Supermarket",
            alamat="Jl. Raya Condongcatur No. 12, Sleman, Yogyakarta",
            lat=-7.7589,
            lng=110.4011,
        )
        db.add(default_store)
        db.flush()
        stores = [default_store]

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
        existing = (
            db.query(AdCampaign)
            .filter(
                AdCampaign.owner_user_id == "00000000-0000-0000-0000-000000000000",
                AdCampaign.payment_status == "demo",
                AdCampaign.title == ad_data["title"],
            )
            .first()
        )
        if existing:
            # Pastikan masa berlaku kampanye demo tetap aktif untuk development
            if _ensure_utc(existing.expires_at) <= now or existing.status != "active":
                existing.status = "active"
                existing.expires_at = now + timedelta(days=ad_data["duration"])
            continue

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
            expires_at=expires,
        )
        db.add(campaign)

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"Failed to auto-seed ad campaigns: {e}")
