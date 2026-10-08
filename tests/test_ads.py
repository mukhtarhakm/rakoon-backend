import os
import sys
import unittest
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

        app.dependency_overrides[get_current_user] = lambda: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        other_claim_res = self.client.post("/ads/claim-store", json={"store_id": str(self.store.id)})
        self.assertEqual(other_claim_res.status_code, 409)

    def test_create_campaign_success(self):
        # Create campaign for 7 days
        payload = {
            "store_id": str(self.store.id),
            "title": "Promo Minyak & Susu Akhir Pekan",
            "banner_url": "https://example.com/promo-flyer.png",
            "duration_days": 7,
            "payment_method": "QRIS"
        }
        denied_res = self.client.post("/ads/campaigns", json=payload)
        self.assertEqual(denied_res.status_code, 403)

        claim_res = self.client.post("/ads/claim-store", json={"store_id": str(self.store.id)})
        self.assertEqual(claim_res.status_code, 200)
        still_denied_res = self.client.post("/ads/campaigns", json=payload)
        self.assertEqual(still_denied_res.status_code, 403)

        verify_res = self.client.post("/ads/verify-claim", json={
            "user_id": TEST_USER_ID,
            "store_id": str(self.store.id),
        })
        self.assertEqual(verify_res.status_code, 200)
        res = self.client.post("/ads/campaigns", json=payload)
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data["title"], "Promo Minyak & Susu Akhir Pekan")
        self.assertEqual(data["duration_days"], 7)
        self.assertEqual(data["price_paid"], 30000)
        self.assertGreaterEqual(data["days_left"], 6)

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
        # Create an active campaign
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
            start_at=now - timedelta(days=10),
            expires_at=now - timedelta(days=7)
        )
        unverified_campaign = AdCampaign(
            store_id=self.store.id,
            owner_user_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            title="Promo Pemilik Belum Diverifikasi",
            banner_url="https://example.com/unverified.jpg",
            duration_days=3,
            price_paid=15000,
            status="active",
            start_at=now,
            expires_at=now + timedelta(days=3),
        )
        self.db.add_all([owner, active_campaign, expired_campaign, unverified_campaign])
        self.db.commit()

        # Query home banners near Condongcatur (-7.76, 110.40)
        res = self.client.get("/ads/home-banners?lat=-7.7600&lng=110.4000&radius_km=10")
        self.assertEqual(res.status_code, 200)
        banners = res.json()

        # Only the active campaign should be returned, expired excluded
        titles = [b["title"] for b in banners]
        self.assertIn("Spesial Diskon Sembako Pamela", titles)
        self.assertNotIn("Promo Sudah Lewat", titles)
        self.assertNotIn("Promo Pemilik Belum Diverifikasi", titles)

        # Distance should be small (< 1 km)
        pamela_banner = next(b for b in banners if b["title"] == "Spesial Diskon Sembako Pamela")
        self.assertLess(pamela_banner["distance_km"], 2.0)
        self.assertEqual(pamela_banner["store_nama"], "Pamela 6 Supermarket")


if __name__ == "__main__":
    unittest.main()
