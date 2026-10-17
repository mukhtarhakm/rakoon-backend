"""POST /ads/campaigns idempotent dalam praktik: create identik oleh pemilik yang sama dalam 10 menit
mengembalikan kampanye pending_payment yang sudah ada. Tanpa Xendit; DB SQLite."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Query, sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.dependencies import get_current_user
from app.models.db_models import AdCampaign, Store, StoreOwner

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


USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PAYLOAD = {
    "title": "Promo Akhir Pekan",
    "banner_url": "https://example.com/flyer.png",
    "duration_days": 7,
    "payment_method": "QRIS",
}


class TestCampaignCreateDedup(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: USER_ID
        self.client = TestClient(app)

        self.store = Store(nama="Toko Uji", lat=-7.75, lng=110.4)
        self.db.add(self.store)
        self.db.commit()
        self.db.add(StoreOwner(
            user_id=USER_ID, store_id=self.store.id, status="verified",
            verified_at=datetime.now(timezone.utc),
        ))
        self.db.commit()

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _create(self, **overrides):
        body = {"store_id": str(self.store.id), **PAYLOAD, **overrides}
        return self.client.post("/ads/campaigns", json=body)

    def _count(self):
        self.db.expire_all()
        return self.db.query(AdCampaign).count()

    def _age_all(self, **delta):
        self.db.query(AdCampaign).update({AdCampaign.created_at: datetime.now(timezone.utc) - timedelta(**delta)})
        self.db.commit()

    def test_identical_creates_return_same_campaign(self):
        first, second = self._create(), self._create()
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(first.json()["payment_ref"], second.json()["payment_ref"])
        self.assertEqual(self._count(), 1)

    def test_create_seven_seconds_later_reuses_campaign(self):
        first = self._create()
        self._age_all(seconds=7)
        second = self._create()
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(self._count(), 1)

    def test_whitespace_around_title_is_still_a_duplicate(self):
        self._create()
        self._create(title="  Promo Akhir Pekan  ")
        self.assertEqual(self._count(), 1)

    def test_different_title_creates_new_campaign(self):
        self._create()
        self._create(title="Promo Lain Sekali")
        self.assertEqual(self._count(), 2)

    def test_different_duration_or_banner_creates_new_campaign(self):
        self._create()
        self._create(duration_days=14)
        self._create(banner_url="https://example.com/lain.png")
        self.assertEqual(self._count(), 3)

    def test_create_after_ten_minutes_makes_new_campaign(self):
        first = self._create()
        self._age_all(minutes=10, seconds=1)
        second = self._create()
        self.assertNotEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(self._count(), 2)

    def test_paid_or_active_campaign_is_not_deduplicated(self):
        first = self._create()
        self.db.query(AdCampaign).update({"status": "active", "payment_status": "paid"})
        self.db.commit()
        second = self._create()
        self.assertNotEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(self._count(), 2)

    def test_other_users_campaign_is_not_reused(self):
        self.db.add(AdCampaign(
            store_id=self.store.id, owner_user_id=OTHER_ID, title=PAYLOAD["title"],
            banner_url=PAYLOAD["banner_url"], duration_days=7, price_paid=30000,
            payment_status="unpaid", status="pending_payment",
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        ))
        self.db.commit()
        res = self._create()
        self.assertEqual(res.status_code, 201)
        self.assertEqual(self._count(), 2)

    def test_concurrent_create_that_lost_the_lock_race_reuses_winner(self):
        """Request B menunggu kunci baris toko; saat kunci didapat, A sudah commit. B harus melihat A."""
        winner = {}
        real_lock = Query.with_for_update

        def lock_then_rival_commits(query, *args, **kwargs):
            if not winner and query.column_descriptions[0]["entity"] is Store:
                rival = TestingSessionLocal()
                try:
                    row = AdCampaign(
                        store_id=self.store.id, owner_user_id=USER_ID, title=PAYLOAD["title"],
                        banner_url=PAYLOAD["banner_url"], duration_days=7, price_paid=30000,
                        payment_method="QRIS", payment_ref="INV-RIVAL", payment_status="unpaid",
                        status="pending_payment", start_at=datetime.now(timezone.utc),
                        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
                    )
                    rival.add(row)
                    rival.commit()
                    winner["id"] = str(row.id)
                finally:
                    rival.close()
            return real_lock(query, *args, **kwargs)

        with patch.object(Query, "with_for_update", lock_then_rival_commits):
            res = self._create()

        self.assertTrue(winner, "create harus mengunci baris toko (with_for_update) sebelum cek duplikat")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["id"], winner["id"])
        self.assertEqual(self._count(), 1)


if __name__ == "__main__":
    unittest.main()
