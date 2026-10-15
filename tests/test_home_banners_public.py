"""Public GET /ads/home-banners: no payment data, and the radius filter always applies from the caller's position."""
import os
import sys
import uuid
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
from app.models.db_models import AdCampaign, PaymentTransaction, Store, StoreOwner

engine = create_engine(
    "sqlite:///:memory:",
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


OWNER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SECRET_REF = "inv-secret-ref-123"
PRICE = 30000

YOGYA = (-7.7829, 110.4083)
JAKARTA = (-6.2000, 106.8166)


class TestHomeBannersPublic(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)  # no auth override: the endpoint is public

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _paid_banner(self, name, lat, lng, owner=None):
        owner = owner or str(uuid.uuid4())
        store = Store(nama=name, alamat="Jl. Contoh", lat=lat, lng=lng)
        self.db.add(store)
        self.db.commit()
        now = datetime.now(timezone.utc)
        self.db.add(StoreOwner(user_id=owner, store_id=store.id, status="verified", verified_at=now))
        campaign = AdCampaign(
            store_id=store.id, owner_user_id=owner, title=f"Promo {name}", banner_url="https://example.com/b.jpg",
            duration_days=7, price_paid=PRICE, expires_at=now + timedelta(days=5), status="active",
            payment_status="paid", payment_method="QRIS", payment_ref=SECRET_REF,
        )
        self.db.add(campaign)
        self.db.commit()
        self.db.add(PaymentTransaction(
            campaign_id=campaign.id, owner_user_id=owner, provider="xendit", external_id=f"ext-{uuid.uuid4()}",
            amount=PRICE, status="PAID",
        ))
        self.db.commit()
        return campaign

    def _get(self, lat, lng, radius=15):
        res = self.client.get("/ads/home-banners", params={"lat": lat, "lng": lng, "radius_km": radius})
        self.assertEqual(res.status_code, 200)
        return res

    # ---- no payment data on the public response ----
    def test_public_banner_hides_payment_ref_and_price_paid(self):
        self._paid_banner("Toko Jogja", *YOGYA)
        res = self._get(*YOGYA)
        items = res.json()
        self.assertEqual(len(items), 1)
        for forbidden in ("payment_ref", "price_paid"):
            self.assertNotIn(forbidden, items[0])
        self.assertNotIn(SECRET_REF, res.text)
        self.assertNotIn(str(PRICE), res.text.replace("30000.0", ""))
        self.assertEqual(items[0]["title"], "Promo Toko Jogja")  # the useful fields are still there

    def test_owner_still_sees_their_own_payment_data(self):
        self._paid_banner("Toko Jogja", *YOGYA, owner=OWNER)
        app.dependency_overrides[get_current_user] = lambda: OWNER
        mine = self.client.get("/ads/my-store").json()
        self.assertEqual(mine["campaigns"][0]["payment_ref"], SECRET_REF)
        self.assertEqual(mine["campaigns"][0]["price_paid"], PRICE)

    # ---- radius bypass ----
    def test_user_far_from_yogyakarta_does_not_get_yogyakarta_banners(self):
        self._paid_banner("Toko Jogja", *YOGYA)
        self.assertEqual(self._get(*JAKARTA).json(), [])

    def test_user_far_from_yogyakarta_gets_only_nearby_banners_with_real_distance(self):
        self._paid_banner("Toko Jogja", *YOGYA)
        self._paid_banner("Toko Jakarta", -6.2100, 106.8200)
        items = self._get(*JAKARTA).json()
        self.assertEqual([i["store_nama"] for i in items], ["Toko Jakarta"])
        self.assertLess(items[0]["distance_km"], 5)

    def test_radius_is_respected_near_yogyakarta_too(self):
        self._paid_banner("Toko Jauh", -7.7829 - 0.18, 110.4083)  # about 20 km south
        self.assertEqual(self._get(*YOGYA, radius=15).json(), [])
        self.assertEqual(len(self._get(*YOGYA, radius=25).json()), 1)

    def test_nan_position_does_not_skip_the_radius_filter(self):
        self._paid_banner("Toko Jogja", *YOGYA)
        res = self.client.get("/ads/home-banners", params={"lat": "nan", "lng": "nan", "radius_km": 15})
        self.assertIn(res.status_code, (200, 422))
        if res.status_code == 200:
            self.assertEqual(res.json(), [])


if __name__ == "__main__":
    unittest.main()
