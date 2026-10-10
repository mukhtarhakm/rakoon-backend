import os
import sys
import unittest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.dependencies import get_current_user, get_current_admin_user
from app.models.db_models import Store, StoreOwner, AdCampaign

SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


TEST_USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_USER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class TestAdsModule(unittest.TestCase):

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: TEST_USER_ID
        app.dependency_overrides[get_current_admin_user] = lambda: {"user_id": "admin"}
        self.client = TestClient(app)

        # Seed sample store in Yogyakarta
        self.store = Store(
            nama="Pamela 6 Supermarket",
            alamat="Jl. Raya Condongcatur No. 12, Sleman, Yogyakarta",
            lat=-7.7589,
            lng=110.4011
        )
        self.db.add(self.store)
        self.db.commit()
        self.db.refresh(self.store)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_get_pricing_packages(self):
        res = self.client.get("/ads/pricing-packages")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 3)
        self.assertEqual(data[0]["duration_days"], 3)
        self.assertEqual(data[0]["price"], 15000)
        self.assertEqual(data[1]["duration_days"], 7)
        self.assertEqual(data[1]["price"], 30000)
        self.assertEqual(data[2]["duration_days"], 14)
        self.assertEqual(data[2]["price"], 50000)

    def test_claim_store_and_get_my_store(self):
        # Claim store
        claim_res = self.client.post("/ads/claim-store", json={"store_id": str(self.store.id)})
        self.assertEqual(claim_res.status_code, 200)
        claim_data = claim_res.json()
        self.assertFalse(claim_data["is_claimed"])
        self.assertEqual(claim_data["claim_status"], "pending")
        self.assertEqual(claim_data["store_id"], str(self.store.id))
        self.assertEqual(claim_data["store_nama"], "Pamela 6 Supermarket")

        # Get my store
        my_res = self.client.get("/ads/my-store")
        self.assertEqual(my_res.status_code, 200)
        my_data = my_res.json()
        self.assertFalse(my_data["is_claimed"])
        self.assertEqual(my_data["claim_status"], "pending")
        self.assertEqual(my_data["store_id"], str(self.store.id))

        pending_res = self.client.get("/ads/pending-claims")
        self.assertEqual(pending_res.status_code, 200)
        self.assertEqual(len(pending_res.json()), 1)
        self.assertEqual(pending_res.json()[0]["user_id"], TEST_USER_ID)

        app.dependency_overrides.pop(get_current_admin_user)
        self.assertEqual(self.client.get("/ads/pending-claims").status_code, 403)
        self.assertEqual(self.client.post("/ads/verify-claim", json={
            "user_id": TEST_USER_ID,
            "store_id": str(self.store.id),
        }).status_code, 403)
        app.dependency_overrides[get_current_admin_user] = lambda: {"user_id": "admin"}

        verify_res = self.client.post("/ads/verify-claim", json={
            "user_id": TEST_USER_ID,
            "store_id": str(self.store.id),
        })
        self.assertEqual(verify_res.status_code, 200)
        self.assertTrue(verify_res.json()["is_claimed"])
        self.assertEqual(verify_res.json()["claim_status"], "verified")
        self.assertEqual(self.client.get("/ads/pending-claims").json(), [])

        app.dependency_overrides[get_current_user] = lambda: OTHER_USER_ID
        other_claim_res = self.client.post("/ads/claim-store", json={"store_id": str(self.store.id)})
        self.assertEqual(other_claim_res.status_code, 409)

    def test_admin_claim_approval_rejection_and_authorization(self):
        """Verify admin claim list, approval, rejection, and strict authorization guards."""
        # 1. User claims store
        claim_res = self.client.post("/ads/claim-store", json={"store_id": str(self.store.id)})
        self.assertEqual(claim_res.status_code, 200)

        # 2. Non-admin (or unauthenticated) forbidden from admin claim endpoints
        app.dependency_overrides.pop(get_current_admin_user, None)
        self.assertEqual(self.client.get("/ads/pending-claims").status_code, 403)
        self.assertEqual(self.client.post("/ads/verify-claim", json={"user_id": TEST_USER_ID, "store_id": str(self.store.id)}).status_code, 403)
        self.assertEqual(self.client.post("/ads/reject-claim", json={"user_id": TEST_USER_ID, "store_id": str(self.store.id)}).status_code, 403)

        # 3. Admin sees pending claim with store details
        app.dependency_overrides[get_current_admin_user] = lambda: {"user_id": "admin-1", "role": "admin"}
        pending_res = self.client.get("/ads/pending-claims")
        self.assertEqual(pending_res.status_code, 200)
        claims = pending_res.json()
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["user_id"], TEST_USER_ID)
        self.assertEqual(claims[0]["store_id"], str(self.store.id))
        self.assertEqual(claims[0]["store_nama"], self.store.nama)

        # 4. Reject non-existent claim returns 404
        self.assertEqual(self.client.post("/ads/reject-claim", json={"user_id": "non-existent", "store_id": str(self.store.id)}).status_code, 404)
        self.assertEqual(self.client.post("/ads/verify-claim", json={"user_id": "non-existent", "store_id": str(self.store.id)}).status_code, 404)

        # 5. Admin rejects claim
        reject_res = self.client.post("/ads/reject-claim", json={"user_id": TEST_USER_ID, "store_id": str(self.store.id), "reason": "Dokumen tidak lengkap"})
        self.assertEqual(reject_res.status_code, 200)
        self.assertFalse(reject_res.json()["is_claimed"])
        self.assertEqual(reject_res.json()["claim_status"], "rejected")

        # 5a. Rejected claim cannot be verified directly or rejected again
        verify_rejected = self.client.post("/ads/verify-claim", json={"user_id": TEST_USER_ID, "store_id": str(self.store.id)})
        self.assertEqual(verify_rejected.status_code, 400)
        reject_again = self.client.post("/ads/reject-claim", json={"user_id": TEST_USER_ID, "store_id": str(self.store.id)})
        self.assertEqual(reject_again.status_code, 400)

        # 6. Rejected claim is removed from pending claims
        self.assertEqual(self.client.get("/ads/pending-claims").json(), [])

        # 7. Merchant checks status: reflects rejected
        my_store = self.client.get("/ads/my-store")
        self.assertEqual(my_store.status_code, 200)
        self.assertEqual(my_store.json()["claim_status"], "rejected")
        self.assertFalse(my_store.json()["is_claimed"])

        # 8. Merchant can re-claim and admin approves it
        reclaim_res = self.client.post("/ads/claim-store", json={"store_id": str(self.store.id)})
        self.assertEqual(reclaim_res.status_code, 200)
        self.assertEqual(len(self.client.get("/ads/pending-claims").json()), 1)

        verify_res = self.client.post("/ads/verify-claim", json={"user_id": TEST_USER_ID, "store_id": str(self.store.id)})
        self.assertEqual(verify_res.status_code, 200)
        self.assertTrue(verify_res.json()["is_claimed"])
        self.assertEqual(verify_res.json()["claim_status"], "verified")

        # 9. Already verified claim cannot be rejected or re-verified
        reject_verified = self.client.post("/ads/reject-claim", json={"user_id": TEST_USER_ID, "store_id": str(self.store.id)})
        self.assertEqual(reject_verified.status_code, 400)
        verify_again = self.client.post("/ads/verify-claim", json={"user_id": TEST_USER_ID, "store_id": str(self.store.id)})
        self.assertEqual(verify_again.status_code, 400)

    def test_pending_claims_visibility_of_conflict_with_verified_owner(self):
        """When a store is already verified to one merchant, other pending claims for it show has_verified_owner=True."""
        # 1. First user claims and is verified
        self.client.post("/ads/claim-store", json={"store_id": str(self.store.id)})
        self.assertEqual(self.client.post("/ads/verify-claim", json={"user_id": TEST_USER_ID, "store_id": str(self.store.id)}).status_code, 200)

        # 2. Concurrently existing pending claim from second user for the same store
        other_claim = StoreOwner(
            user_id=OTHER_USER_ID,
            store_id=self.store.id,
            status="pending",
        )
        self.db.add(other_claim)
        self.db.commit()

        # 3. Admin gets pending claims: other_claim has has_verified_owner=True
        app.dependency_overrides[get_current_admin_user] = lambda: {"user_id": "admin"}
        pending_res = self.client.get("/ads/pending-claims")
        self.assertEqual(pending_res.status_code, 200)
        claims = pending_res.json()
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["user_id"], OTHER_USER_ID)
        self.assertTrue(claims[0]["has_verified_owner"])

        # 4. Attempting to verify other_claim fails with 409 Conflict
        verify_conflict = self.client.post("/ads/verify-claim", json={"user_id": OTHER_USER_ID, "store_id": str(self.store.id)})
        self.assertEqual(verify_conflict.status_code, 409)

        # 5. Admin can reject other_claim cleanly
        reject_res = self.client.post("/ads/reject-claim", json={"user_id": OTHER_USER_ID, "store_id": str(self.store.id)})
        self.assertEqual(reject_res.status_code, 200)
        self.assertEqual(self.client.get("/ads/pending-claims").json(), [])

    def test_create_campaign_starts_as_pending_payment_and_unpaid(self):
        """Campaign created without verified payment must start strictly as pending_payment and unpaid."""
        # Claim and verify store
        self.client.post("/ads/claim-store", json={"store_id": str(self.store.id)})
        self.client.post("/ads/verify-claim", json={
            "user_id": TEST_USER_ID,
            "store_id": str(self.store.id),
        })

        payload = {
            "store_id": str(self.store.id),
            "title": "Promo Minyak & Susu Akhir Pekan",
            "banner_url": "https://example.com/promo-flyer.png",
            "duration_days": 7,
            "payment_method": "QRIS"
        }
        res = self.client.post("/ads/campaigns", json=payload)
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data["title"], "Promo Minyak & Susu Akhir Pekan")
        self.assertEqual(data["duration_days"], 7)
        self.assertEqual(data["price_paid"], 30000)
        self.assertEqual(data["status"], "pending_payment")
        self.assertEqual(data["payment_status"], "unpaid")
        self.assertTrue(data["payment_ref"].startswith("INV-"))

        # Verify database record
        campaign_db = self.db.query(AdCampaign).filter(AdCampaign.id == data["id"]).first()
        self.assertIsNotNone(campaign_db)
        self.assertEqual(campaign_db.status, "pending_payment")
        self.assertEqual(campaign_db.payment_status, "unpaid")

    def test_client_cannot_inject_payment_ref_as_proof(self):
        """Client supplying a fake payment_ref must have it ignored and cannot gain paid status."""
        self.client.post("/ads/claim-store", json={"store_id": str(self.store.id)})
        self.client.post("/ads/verify-claim", json={
            "user_id": TEST_USER_ID,
            "store_id": str(self.store.id),
        })

        payload = {
            "store_id": str(self.store.id),
            "title": "Promo Injeksi Ref",
            "banner_url": "https://example.com/banner.png",
            "duration_days": 3,
            "payment_method": "QRIS",
            "payment_ref": "FAKE-QRIS-SETTLED-SUCCESS"
        }
        res = self.client.post("/ads/campaigns", json=payload)
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data["payment_status"], "unpaid")
        self.assertEqual(data["status"], "pending_payment")
        self.assertNotEqual(data["payment_ref"], "FAKE-QRIS-SETTLED-SUCCESS")
        self.assertTrue(data["payment_ref"].startswith("INV-"))

    def test_client_cannot_force_active_or_paid_status(self):
        """Client attempting to inject status=active or payment_status=paid in request body must be ignored."""
        self.client.post("/ads/claim-store", json={"store_id": str(self.store.id)})
        self.client.post("/ads/verify-claim", json={
            "user_id": TEST_USER_ID,
            "store_id": str(self.store.id),
        })

        payload = {
            "store_id": str(self.store.id),
            "title": "Promo Hacked Status",
            "banner_url": "https://example.com/banner.png",
            "duration_days": 3,
            "status": "active",
            "payment_status": "paid"
        }
        res = self.client.post("/ads/campaigns", json=payload)
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data["status"], "pending_payment")
        self.assertEqual(data["payment_status"], "unpaid")

    def test_pending_payment_campaign_excluded_from_home_banners(self):
        """Campaigns that are not active or unpaid must never appear on public home banners."""
        self.client.post("/ads/claim-store", json={"store_id": str(self.store.id)})
        self.client.post("/ads/verify-claim", json={
            "user_id": TEST_USER_ID,
            "store_id": str(self.store.id),
        })

        # Create an unpaid pending campaign
        create_res = self.client.post("/ads/campaigns", json={
            "store_id": str(self.store.id),
            "title": "Promo Belum Dibayar",
            "banner_url": "https://example.com/unpaid.png",
            "duration_days": 3,
        })
        self.assertEqual(create_res.status_code, 201)
        unpaid_id = create_res.json()["id"]

        # Fetch home banners
        res = self.client.get("/ads/home-banners?lat=-7.7589&lng=110.4011&radius_km=10")
        self.assertEqual(res.status_code, 200)
        banner_ids = [b["id"] for b in res.json()]
        self.assertNotIn(unpaid_id, banner_ids)

    def test_merchant_cannot_access_other_merchant_campaign(self):
        """Merchant A cannot view or inspect campaign belonging to Merchant B."""
        # 1. Merchant A creates campaign
        self.client.post("/ads/claim-store", json={"store_id": str(self.store.id)})
        self.client.post("/ads/verify-claim", json={
            "user_id": TEST_USER_ID,
            "store_id": str(self.store.id),
        })
        create_res = self.client.post("/ads/campaigns", json={
            "store_id": str(self.store.id),
            "title": "Promo Merchant A",
            "banner_url": "https://example.com/a.png",
            "duration_days": 3,
        })
        campaign_a_id = create_res.json()["id"]

        # 2. Merchant A can view it
        get_res_a = self.client.get(f"/ads/campaigns/{campaign_a_id}")
        self.assertEqual(get_res_a.status_code, 200)
        self.assertEqual(get_res_a.json()["id"], campaign_a_id)

        # 3. Switch to Merchant B
        app.dependency_overrides[get_current_user] = lambda: OTHER_USER_ID
        get_res_b = self.client.get(f"/ads/campaigns/{campaign_a_id}")
        self.assertEqual(get_res_b.status_code, 403)
        self.assertIn("tidak memiliki izin", get_res_b.json()["detail"])

    def test_unconfigured_payment_provider_fails_safely(self):
        """Calling pay endpoint when payment provider is unconfigured returns 503 fail-closed."""
        self.client.post("/ads/claim-store", json={"store_id": str(self.store.id)})
        self.client.post("/ads/verify-claim", json={
            "user_id": TEST_USER_ID,
            "store_id": str(self.store.id),
        })
        create_res = self.client.post("/ads/campaigns", json={
            "store_id": str(self.store.id),
            "title": "Promo Siap Bayar",
            "banner_url": "https://example.com/banner.png",
            "duration_days": 3,
        })
        campaign_id = create_res.json()["id"]

        # Ensure XENDIT_SECRET_KEY is unset
        old_key = os.environ.pop("XENDIT_SECRET_KEY", None)
        try:
            pay_res = self.client.post(f"/ads/campaigns/{campaign_id}/pay")
            self.assertEqual(pay_res.status_code, 503)
            self.assertIn("belum dikonfigurasi", pay_res.json()["detail"])
        finally:
            if old_key is not None:
                os.environ["XENDIT_SECRET_KEY"] = old_key

    def test_verified_owner_cannot_advertise_another_store(self):
        other_store = Store(nama="Toko Lain", lat=-7.76, lng=110.4)
        self.db.add(other_store)
        self.db.commit()
        self.db.refresh(other_store)

        self.client.post("/ads/claim-store", json={"store_id": str(self.store.id)})
        self.client.post("/ads/verify-claim", json={
            "user_id": TEST_USER_ID,
            "store_id": str(self.store.id),
        })
        res = self.client.post("/ads/campaigns", json={
            "store_id": str(other_store.id),
            "title": "Promo Toko Lain",
            "banner_url": "https://example.com/banner.png",
            "duration_days": 3,
        })
        self.assertEqual(res.status_code, 403)

    def test_unverified_user_cannot_upload_banner(self):
        res = self.client.post(
            "/ads/upload-banner",
            files={"file": ("banner.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        )
        self.assertEqual(res.status_code, 403)

    def test_legacy_verified_claim_requires_admin_reverification(self):
        self.db.add(StoreOwner(
            user_id=TEST_USER_ID,
            store_id=self.store.id,
            status="verified",
        ))
        self.db.commit()

        my_store = self.client.get("/ads/my-store")
        self.assertEqual(my_store.status_code, 200)
        self.assertFalse(my_store.json()["is_claimed"])
        self.assertEqual(my_store.json()["claim_status"], "pending")
        self.assertEqual(len(self.client.get("/ads/pending-claims").json()), 1)

        payload = {
            "store_id": str(self.store.id),
            "title": "Promo Toko Lama",
            "banner_url": "https://example.com/banner.png",
            "duration_days": 3,
        }
        self.assertEqual(self.client.post("/ads/campaigns", json=payload).status_code, 403)
        self.assertEqual(self.client.post("/ads/verify-claim", json={
            "user_id": TEST_USER_ID,
            "store_id": str(self.store.id),
        }).status_code, 200)
        self.assertEqual(self.client.post("/ads/campaigns", json=payload).status_code, 201)

    def test_create_campaign_invalid_duration(self):
        self.client.post("/ads/claim-store", json={"store_id": str(self.store.id)})
        self.client.post("/ads/verify-claim", json={
            "user_id": TEST_USER_ID,
            "store_id": str(self.store.id),
        })
        payload = {
            "store_id": str(self.store.id),
            "title": "Promo Tidak Valid",
            "banner_url": "https://example.com/banner.png",
            "duration_days": 5  # Not in [3, 7, 14]
        }
        res = self.client.post("/ads/campaigns", json=payload)
        self.assertEqual(res.status_code, 400)
        self.assertIn("Durasi tidak valid", res.json()["detail"])

    def test_get_home_banners_active_and_nearby(self):
        # Create an active and paid campaign
        now = datetime.now(timezone.utc)
        owner = StoreOwner(
            user_id=TEST_USER_ID,
            store_id=self.store.id,
            status="verified",
            verified_at=now,
        )
        active_campaign = AdCampaign(
            store_id=self.store.id,
            owner_user_id=TEST_USER_ID,
            title="Spesial Diskon Sembako Pamela",
            banner_url="https://example.com/pamela-flyer.jpg",
            duration_days=3,
            price_paid=15000,
            status="active",
            payment_status="paid",
            payment_ref="XND-20261010-PAMELA01",
            payment_method="XENDIT_QRIS",
            start_at=now,
            expires_at=now + timedelta(days=3)
        )
        # Create an expired campaign
        expired_campaign = AdCampaign(
            store_id=self.store.id,
            owner_user_id=TEST_USER_ID,
            title="Promo Sudah Lewat",
            banner_url="https://example.com/old.jpg",
            duration_days=3,
            price_paid=15000,
            status="active",
            payment_status="paid",
            start_at=now - timedelta(days=10),
            expires_at=now - timedelta(days=7)
        )
        unverified_campaign = AdCampaign(
            store_id=self.store.id,
            owner_user_id=OTHER_USER_ID,
            title="Promo Pemilik Belum Diverifikasi",
            banner_url="https://example.com/unverified.jpg",
            duration_days=3,
            price_paid=15000,
            status="active",
            payment_status="paid",
            payment_ref="XND-20261010-PAMELA01",
            payment_method="XENDIT_QRIS",
            start_at=now,
            expires_at=now + timedelta(days=3),
        )
        self.db.add_all([owner, active_campaign, expired_campaign, unverified_campaign])
        self.db.commit()

        # Query home banners near Condongcatur (-7.76, 110.40)
        # Using mock provider verification to validate distance & expiry filters independently
        with patch("app.routers.ads._has_verified_provider_proof", side_effect=lambda c: getattr(c, "payment_ref", "") == "XND-20261010-PAMELA01"):
            res = self.client.get("/ads/home-banners?lat=-7.7600&lng=110.4000&radius_km=10")
            self.assertEqual(res.status_code, 200)
            banners = res.json()

        # Only the active and verified campaign should be returned, expired excluded
        titles = [b["title"] for b in banners]
        self.assertIn("Spesial Diskon Sembako Pamela", titles)
        self.assertNotIn("Promo Sudah Lewat", titles)
        self.assertNotIn("Promo Pemilik Belum Diverifikasi", titles)

        # Distance should be small (< 1 km)
        pamela_banner = next(b for b in banners if b["title"] == "Spesial Diskon Sembako Pamela")
        self.assertLess(pamela_banner["distance_km"], 2.0)
        self.assertEqual(pamela_banner["store_nama"], "Pamela 6 Supermarket")


    def test_legacy_active_paid_campaign_without_provider_proof_is_excluded(self):
        """
        Security: Legacy campaigns with status='active' and payment_status='paid' but lacking
        verified official provider proof (e.g. payment_ref is None, starts with INV- or QRIS-)
        must NEVER be served as commercial paid advertisements on home banners.
        """
        now = datetime.now(timezone.utc)
        owner = StoreOwner(
            user_id=TEST_USER_ID,
            store_id=self.store.id,
            status="verified",
            verified_at=now,
        )
        legacy_no_ref = AdCampaign(
            store_id=self.store.id,
            owner_user_id=TEST_USER_ID,
            title="Iklan Lama Tanpa Provider Ref",
            banner_url="https://example.com/legacy1.jpg",
            duration_days=3,
            price_paid=15000,
            status="active",
            payment_status="paid",
            payment_ref=None,
            start_at=now,
            expires_at=now + timedelta(days=3),
        )
        legacy_fake_qris = AdCampaign(
            store_id=self.store.id,
            owner_user_id=TEST_USER_ID,
            title="Iklan Lama Fake QRIS Ref",
            banner_url="https://example.com/legacy2.jpg",
            duration_days=3,
            price_paid=15000,
            status="active",
            payment_status="paid",
            payment_ref="QRIS-20261010-FAKE99",
            start_at=now,
            expires_at=now + timedelta(days=3),
        )
        legacy_internal_inv = AdCampaign(
            store_id=self.store.id,
            owner_user_id=TEST_USER_ID,
            title="Iklan Invoice Internal Unpaid",
            banner_url="https://example.com/legacy3.jpg",
            duration_days=3,
            price_paid=15000,
            status="active",
            payment_status="paid",
            payment_ref="INV-20261010-ABCD1234",
            start_at=now,
            expires_at=now + timedelta(days=3),
        )
        self.db.add_all([owner, legacy_no_ref, legacy_fake_qris, legacy_internal_inv])
        self.db.commit()

        res = self.client.get("/ads/home-banners?lat=-7.7600&lng=110.4000&radius_km=10")
        self.assertEqual(res.status_code, 200)
        titles = [b["title"] for b in res.json()]
        self.assertNotIn("Iklan Lama Tanpa Provider Ref", titles)
        self.assertNotIn("Iklan Lama Fake QRIS Ref", titles)
        self.assertNotIn("Iklan Invoice Internal Unpaid", titles)

    def test_fake_xendit_reference_prefix_rejected_fail_closed(self):
        """
        Security: String prefixes like 'XND-' or 'XENDIT-' are NOT trusted as payment proof.
        Campaigns with fake or unverified provider prefix must remain fail-closed and excluded.
        """
        now = datetime.now(timezone.utc)
        owner = StoreOwner(
            user_id=TEST_USER_ID,
            store_id=self.store.id,
            status="verified",
            verified_at=now,
        )
        forged_xnd = AdCampaign(
            store_id=self.store.id,
            owner_user_id=TEST_USER_ID,
            title="Iklan Palsu Prefix XND",
            banner_url="https://example.com/forged1.jpg",
            duration_days=3,
            price_paid=15000,
            status="active",
            payment_status="paid",
            payment_ref="XND-FORGED-FAKE-REF-001",
            start_at=now,
            expires_at=now + timedelta(days=3),
        )
        forged_xendit = AdCampaign(
            store_id=self.store.id,
            owner_user_id=TEST_USER_ID,
            title="Iklan Palsu Prefix XENDIT",
            banner_url="https://example.com/forged2.jpg",
            duration_days=3,
            price_paid=15000,
            status="active",
            payment_status="paid",
            payment_ref="XENDIT-FORGED-FAKE-REF-002",
            start_at=now,
            expires_at=now + timedelta(days=3),
        )
        self.db.add_all([owner, forged_xnd, forged_xendit])
        self.db.commit()

        res = self.client.get("/ads/home-banners?lat=-7.7600&lng=110.4000&radius_km=10")
        self.assertEqual(res.status_code, 200)
        titles = [b["title"] for b in res.json()]
        self.assertNotIn("Iklan Palsu Prefix XND", titles)
        self.assertNotIn("Iklan Palsu Prefix XENDIT", titles)

    def test_include_demo_alone_does_not_enable_demo_on_production(self):
        """
        Security: Calling ?include_demo=true on production must NEVER activate demo ads.
        Demo mode must be completely locked down on production.
        """
        now = datetime.now(timezone.utc)
        demo_campaign = AdCampaign(
            store_id=self.store.id,
            owner_user_id="00000000-0000-0000-0000-000000000000",
            title="Banner Demo Terlarang di Produksi",
            banner_url="https://example.com/demo-prod.jpg",
            duration_days=3,
            price_paid=15000,
            status="active",
            payment_status="demo",
            payment_method="DEMO",
            start_at=now,
            expires_at=now + timedelta(days=3),
        )
        self.db.add(demo_campaign)
        self.db.commit()

        old_env = os.environ.get("ENVIRONMENT")
        old_allow = os.environ.get("ALLOW_DEMO_ADS")
        try:
            os.environ["ENVIRONMENT"] = "production"
            os.environ["ALLOW_DEMO_ADS"] = "true"

            res = self.client.get("/ads/home-banners?lat=-7.7600&lng=110.4000&radius_km=10&include_demo=true")
            self.assertEqual(res.status_code, 200)
            titles = [b["title"] for b in res.json()]
            self.assertNotIn("Banner Demo Terlarang di Produksi", titles)
        finally:
            if old_env is not None:
                os.environ["ENVIRONMENT"] = old_env
            else:
                os.environ.pop("ENVIRONMENT", None)
            if old_allow is not None:
                os.environ["ALLOW_DEMO_ADS"] = old_allow
            else:
                os.environ.pop("ALLOW_DEMO_ADS", None)

    def test_include_demo_alone_without_server_config_is_rejected(self):
        """
        Security: Client request parameter include_demo=true alone cannot enable demo ads
        without explicit server environment configuration ALLOW_DEMO_ADS='true'.
        """
        now = datetime.now(timezone.utc)
        demo_campaign = AdCampaign(
            store_id=self.store.id,
            owner_user_id="00000000-0000-0000-0000-000000000000",
            title="Banner Demo Tanpa Izin Server",
            banner_url="https://example.com/demo.jpg",
            duration_days=3,
            price_paid=15000,
            status="active",
            payment_status="demo",
            payment_method="DEMO",
            start_at=now,
            expires_at=now + timedelta(days=3),
        )
        self.db.add(demo_campaign)
        self.db.commit()

        old_allow = os.environ.pop("ALLOW_DEMO_ADS", None)
        try:
            res = self.client.get("/ads/home-banners?lat=-7.7600&lng=110.4000&radius_km=10&include_demo=true")
            self.assertEqual(res.status_code, 200)
            titles = [b["title"] for b in res.json()]
            self.assertNotIn("Banner Demo Tanpa Izin Server", titles)
        finally:
            if old_allow is not None:
                os.environ["ALLOW_DEMO_ADS"] = old_allow

    def test_demo_ads_only_shown_when_server_allows_and_client_requests(self):
        """
        Security: Demo sample campaigns are returned only when server ALLOW_DEMO_ADS='true'
        AND client requests include_demo=true, and are tagged with payment_status='demo',
        never disguised as commercial paid advertisements.
        """
        now = datetime.now(timezone.utc)
        demo_campaign = AdCampaign(
            store_id=self.store.id,
            owner_user_id="00000000-0000-0000-0000-000000000000",
            title="Demo Banner Flyer Jogja",
            banner_url="https://example.com/demo.jpg",
            duration_days=3,
            price_paid=15000,
            status="active",
            payment_status="demo",
            payment_method="DEMO",
            start_at=now,
            expires_at=now + timedelta(days=3),
        )
        self.db.add(demo_campaign)
        self.db.commit()

        old_allow = os.environ.get("ALLOW_DEMO_ADS")
        old_env = os.environ.get("ENVIRONMENT")
        try:
            os.environ["ALLOW_DEMO_ADS"] = "true"
            os.environ["ENVIRONMENT"] = "development"

            # 1. Without include_demo query -> demo ads are excluded
            default_res = self.client.get("/ads/home-banners?lat=-7.7600&lng=110.4000&radius_km=10")
            self.assertEqual(default_res.status_code, 200)
            self.assertNotIn("Demo Banner Flyer Jogja", [b["title"] for b in default_res.json()])

            # 2. With include_demo=true -> demo ads returned, explicitly tagged
            demo_res = self.client.get("/ads/home-banners?lat=-7.7600&lng=110.4000&radius_km=10&include_demo=true")
            self.assertEqual(demo_res.status_code, 200)
            demo_banners = demo_res.json()
            demo_match = next((b for b in demo_banners if b["title"] == "Demo Banner Flyer Jogja"), None)
            self.assertIsNotNone(demo_match)
            self.assertEqual(demo_match["payment_status"], "demo")
            self.assertEqual(demo_match["payment_method"], "DEMO")
        finally:
            if old_allow is not None:
                os.environ["ALLOW_DEMO_ADS"] = old_allow
            else:
                os.environ.pop("ALLOW_DEMO_ADS", None)
            if old_env is not None:
                os.environ["ENVIRONMENT"] = old_env
            else:
                os.environ.pop("ENVIRONMENT", None)

    def test_pay_endpoint_fails_closed_even_with_xendit_key(self):
        """
        Security: Calling initiate payment when XENDIT_SECRET_KEY is present still returns
        HTTP 503 fail-closed and NEVER activates or marks the campaign as paid prematurely.
        """
        self.client.post("/ads/claim-store", json={"store_id": str(self.store.id)})
        self.client.post("/ads/verify-claim", json={
            "user_id": TEST_USER_ID,
            "store_id": str(self.store.id),
        })
        create_res = self.client.post("/ads/campaigns", json={
            "store_id": str(self.store.id),
            "title": "Promo Belum Terintegrasi",
            "banner_url": "https://example.com/banner.png",
            "duration_days": 3,
        })
        campaign_id = create_res.json()["id"]

        old_key = os.environ.get("XENDIT_SECRET_KEY")
        try:
            os.environ["XENDIT_SECRET_KEY"] = "xnd_development_fake_secret_key_12345"
            pay_res = self.client.post(f"/ads/campaigns/{campaign_id}/pay")
            self.assertIn(pay_res.status_code, (502, 503))

            # Verify in DB that campaign status remains pending_payment and unpaid
            campaign = self.db.query(AdCampaign).filter(AdCampaign.id == campaign_id).first()
            self.assertEqual(campaign.status, "pending_payment")
            self.assertEqual(campaign.payment_status, "unpaid")
        finally:
            if old_key is not None:
                os.environ["XENDIT_SECRET_KEY"] = old_key
            else:
                os.environ.pop("XENDIT_SECRET_KEY", None)

if __name__ == "__main__":
    unittest.main()