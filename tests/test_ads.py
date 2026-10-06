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
from app.dependencies import get_current_user
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
        self.assertTrue(claim_data["is_claimed"])
        self.assertEqual(claim_data["store_id"], str(self.store.id))
        self.assertEqual(claim_data["store_nama"], "Pamela 6 Supermarket")

        # Get my store
        my_res = self.client.get("/ads/my-store")
        self.assertEqual(my_res.status_code, 200)
        my_data = my_res.json()
        self.assertTrue(my_data["is_claimed"])
        self.assertEqual(my_data["store_id"], str(self.store.id))

    def test_create_campaign_success(self):
        # Create campaign for 7 days
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
        self.assertGreaterEqual(data["days_left"], 6)

    def test_create_campaign_invalid_duration(self):
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
        self.db.add_all([active_campaign, expired_campaign])
        self.db.commit()

        # Query home banners near Condongcatur (-7.76, 110.40)
        res = self.client.get("/ads/home-banners?lat=-7.7600&lng=110.4000&radius_km=10")
        self.assertEqual(res.status_code, 200)
        banners = res.json()

        # Only the active campaign should be returned, expired excluded
        titles = [b["title"] for b in banners]
        self.assertIn("Spesial Diskon Sembako Pamela", titles)
        self.assertNotIn("Promo Sudah Lewat", titles)

        # Distance should be small (< 1 km)
        pamela_banner = next(b for b in banners if b["title"] == "Spesial Diskon Sembako Pamela")
        self.assertLess(pamela_banner["distance_km"], 2.0)
        self.assertEqual(pamela_banner["store_nama"], "Pamela 6 Supermarket")


if __name__ == "__main__":
    unittest.main()
