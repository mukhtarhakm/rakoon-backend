import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.dependencies import get_current_user
from app.models.db_models import PriceEntry, Product, ScanSession, Store

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


USER_A = "11111111-1111-1111-1111-111111111111"
USER_B = "22222222-2222-2222-2222-222222222222"


class TestConfirmDuplicateEntries(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        self.current_user = USER_A
        app.dependency_overrides[get_current_user] = lambda: self.current_user
        self.client = TestClient(app)
        self.store = Store(nama="Toko Uji", lat=-7.75, lng=110.4)
        self.other_store = Store(nama="Toko Lain", lat=-7.76, lng=110.41)
        self.db.add_all([self.store, self.other_store])
        self.db.commit()
        self.store_id = str(self.store.id)
        self.other_store_id = str(self.other_store.id)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _item(self, nama="Teh Uji", harga=5000, ukuran=350.0, satuan="ml"):
        return {"nama_produk": nama, "harga": harga, "ukuran": ukuran, "satuan": satuan, "kategori": "Minuman"}

    def _confirm(self, items, store_id=None):
        return self.client.post("/scan/confirm", json={
            "store_id": store_id or self.store_id, "user_id": "diabaikan", "items": items,
        })

    def _entry_count(self):
        self.db.expire_all()
        return self.db.query(PriceEntry).count()

    def _age_entries(self, minutes):
        self.db.expire_all()
        for entry in self.db.query(PriceEntry).all():
            entry.timestamp = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        self.db.commit()

    def test_identical_items_in_one_request_are_saved_once(self):
        res = self._confirm([self._item(), self._item(), self._item(nama="teh uji")])
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["items_saved"], 1)
        self.assertEqual(self._entry_count(), 1)
        self.assertIn("2", res.json()["message"])  # jumlah yang dilewati disebut di pesan

    def test_items_differing_in_price_or_variant_in_one_request_are_all_saved(self):
        res = self._confirm([self._item(), self._item(harga=5500), self._item(ukuran=500.0)])
        self.assertEqual(res.json()["items_saved"], 3)
        self.assertEqual(self._entry_count(), 3)

    def test_repeating_the_same_confirmation_within_ten_minutes_adds_nothing(self):
        first = self._confirm([self._item(), self._item(nama="Kopi Uji", harga=9000)])
        self.assertEqual(first.json()["items_saved"], 2)
        sessions_before = self.db.query(ScanSession).count()

        again = self._confirm([self._item(), self._item(nama="Kopi Uji", harga=9000)])
        self.assertEqual(again.status_code, 201)
        self.assertEqual(again.json()["items_saved"], 0)
        self.assertEqual(self._entry_count(), 2)
        self.db.expire_all()
        self.assertEqual(self.db.query(ScanSession).count(), sessions_before)  # tanpa sesi kosong
        self.assertIsNone(again.json()["scan_session_id"])

    def test_partly_repeated_request_saves_only_the_new_entries(self):
        self._confirm([self._item()])
        res = self._confirm([self._item(), self._item(nama="Kopi Uji", harga=9000)])
        self.assertEqual(res.json()["items_saved"], 1)
        self.assertEqual(self._entry_count(), 2)

    def test_different_price_or_store_is_saved_again(self):
        self._confirm([self._item()])
        self.assertEqual(self._confirm([self._item(harga=5100)]).json()["items_saved"], 1)
        self.assertEqual(self._confirm([self._item()], store_id=self.other_store_id).json()["items_saved"], 1)
        self.assertEqual(self._entry_count(), 3)

    def test_same_entry_from_another_user_is_saved(self):
        self._confirm([self._item()])
        self.current_user = USER_B
        self.assertEqual(self._confirm([self._item()]).json()["items_saved"], 1)
        self.assertEqual(self._entry_count(), 2)

    def test_after_the_ten_minute_window_the_same_entry_is_saved_again(self):
        self._confirm([self._item()])
        self._age_entries(9)
        self.assertEqual(self._confirm([self._item()]).json()["items_saved"], 0)
        self._age_entries(11)
        self.assertEqual(self._confirm([self._item()]).json()["items_saved"], 1)
        self.assertEqual(self._entry_count(), 2)

    def test_skipped_entries_do_not_count_toward_the_daily_quota(self):
        with patch("app.routers.scan.DAILY_CONFIRM_ITEM_QUOTA", 3):
            self.assertEqual(self._confirm([self._item()] * 3).status_code, 201)  # 1 tersimpan, 2 dilewati
            self.assertEqual(self._confirm([self._item()]).status_code, 201)  # dilewati, tidak memakai kuota
            self.assertEqual(self._confirm([self._item(harga=5100), self._item(harga=5200)]).status_code, 201)  # total 3
            blocked = self._confirm([self._item(harga=5300)])
            self.assertEqual(blocked.status_code, 429)
            self.assertEqual(self._entry_count(), 3)

    def test_quota_still_rejects_a_whole_request_of_distinct_entries(self):
        with patch("app.routers.scan.DAILY_CONFIRM_ITEM_QUOTA", 2):
            res = self._confirm([self._item(harga=5000), self._item(harga=5100), self._item(harga=5200)])
            self.assertEqual(res.status_code, 429)
            self.assertEqual(self._entry_count(), 0)
            self.db.expire_all()
            self.assertEqual(self.db.query(ScanSession).count(), 0)
            self.assertEqual(self.db.query(Product).count(), 0)


if __name__ == "__main__":
    unittest.main()
