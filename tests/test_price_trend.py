import os
import random
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.models.db_models import PriceEntry, Product, Store
from app.models.schemas import VerificationStatus
from app.services.price_history_service import calculate_price_trend

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


USER = "11111111-1111-1111-1111-111111111111"


def point(ts, store_id, harga):
    return SimpleNamespace(recorded_at=ts, store_id=store_id, harga=harga)


class TestTrendDaysUseWib(unittest.TestCase):
    def test_entry_at_0200_wib_belongs_to_that_wib_date_not_the_previous_utc_date(self):
        # 2026-10-09 19:00 UTC == 2026-10-10 02:00 WIB
        trend = calculate_price_trend([point(datetime(2026, 10, 9, 19, 0, tzinfo=timezone.utc), "s1", 10000)])
        self.assertEqual([t.date for t in trend], ["2026-10-10"])

    def test_wib_midnight_boundary(self):
        trend = calculate_price_trend([
            point(datetime(2026, 10, 10, 16, 59, 59, tzinfo=timezone.utc), "s1", 10000),  # 23:59:59 WIB 10 Okt
            point(datetime(2026, 10, 10, 17, 0, 0, tzinfo=timezone.utc), "s1", 20000),     # 00:00:00 WIB 11 Okt
        ])
        self.assertEqual([(t.date, t.price) for t in trend], [("2026-10-10", 10000), ("2026-10-11", 20000)])

    def test_naive_timestamps_from_the_database_are_treated_as_utc(self):
        trend = calculate_price_trend([point(datetime(2026, 10, 9, 19, 0), "s1", 10000)])
        self.assertEqual([t.date for t in trend], ["2026-10-10"])

    def test_entries_in_the_same_wib_day_are_averaged_even_across_a_utc_day_change(self):
        trend = calculate_price_trend([
            point(datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc), "s1", 10000),  # 01:00 WIB 10 Okt
            point(datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc), "s1", 12000),  # 17:00 WIB 10 Okt
        ])
        self.assertEqual([(t.date, t.price) for t in trend], [("2026-10-10", 11000)])

    def test_other_timezone_offsets_are_converted_not_ignored(self):
        wita = timezone(timedelta(hours=8))
        trend = calculate_price_trend([point(datetime(2026, 10, 10, 1, 0, tzinfo=wita), "s1", 10000)])  # 00:00 WIB 10 Okt
        self.assertEqual([t.date for t in trend], ["2026-10-10"])
        trend = calculate_price_trend([point(datetime(2026, 10, 10, 0, 30, tzinfo=wita), "s1", 10000)])  # 23:30 WIB 9 Okt
        self.assertEqual([t.date for t in trend], ["2026-10-09"])


class TestTrendOrderIsDeterministic(unittest.TestCase):
    def test_output_order_is_by_date_then_store_regardless_of_input_order(self):
        base = datetime(2026, 10, 1, 5, 0, tzinfo=timezone.utc)
        points = [
            point(base + timedelta(days=d), store, 1000 + d * 10 + i)
            for d in range(4) for i, store in enumerate(("store-c", "store-a", "store-b"))
        ]
        expected = [(t.date, str(t.store_id), t.price) for t in calculate_price_trend(points)]
        self.assertEqual(expected, sorted(expected, key=lambda x: (x[0], x[1])))
        for seed in range(5):
            shuffled = points[:]
            random.Random(seed).shuffle(shuffled)
            self.assertEqual([(t.date, str(t.store_id), t.price) for t in calculate_price_trend(shuffled)], expected)

    def test_uuid_and_string_store_ids_sort_consistently(self):
        ids = [uuid.UUID(int=n) for n in (5, 1, 3)]
        ts = datetime(2026, 10, 1, 5, 0, tzinfo=timezone.utc)
        trend = calculate_price_trend([point(ts, i, 1000) for i in ids] + [point(ts, str(uuid.UUID(int=2)), 1000)])
        self.assertEqual([str(t.store_id) for t in trend], sorted(str(t.store_id) for t in trend))


class TestTrendApi(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self.store = Store(nama="Toko Tren", lat=-7.78, lng=110.4)
        self.product = Product(nama="Produk Tren", kategori="Minuman", ukuran=1.0, satuan="l")
        self.db.add_all([self.store, self.product])
        self.db.commit()
        self.url = f"/price/products/{self.product.id}/price-history"

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _add(self, ts, harga):
        self.db.add(PriceEntry(
            product_id=self.product.id, store_id=self.store.id, harga=harga, sumber_user_id=USER,
            status_verifikasi=VerificationStatus.VERIFIED, timestamp=ts,
        ))
        self.db.commit()

    def test_endpoint_groups_a_0200_wib_scan_on_its_wib_date(self):
        self._add(datetime(2026, 10, 9, 19, 0, tzinfo=timezone.utc), 15000)
        body = self.client.get(self.url).json()
        self.assertEqual([p["date"] for p in body["trend"]], ["2026-10-10"])

    def test_start_after_end_is_a_400_not_an_empty_200(self):
        res = self.client.get(self.url, params={"start_date": "2026-10-10T00:00:00", "end_date": "2026-10-01T00:00:00"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("start_date", res.json()["detail"])

    def test_mixed_naive_and_aware_bounds_are_compared_safely(self):
        res = self.client.get(self.url, params={"start_date": "2026-10-10T00:00:00Z", "end_date": "2026-10-01T00:00:00"})
        self.assertEqual(res.status_code, 400)
        ok = self.client.get(self.url, params={"start_date": "2026-10-01T00:00:00", "end_date": "2026-10-10T00:00:00Z"})
        self.assertEqual(ok.status_code, 200)

    def test_start_equal_to_end_is_allowed(self):
        res = self.client.get(self.url, params={"start_date": "2026-10-05T00:00:00", "end_date": "2026-10-05T00:00:00"})
        self.assertEqual(res.status_code, 200)

    def test_only_start_date_filters_instead_of_being_silently_ignored(self):
        self._add(datetime(2026, 9, 1, 5, 0, tzinfo=timezone.utc), 10000)
        self._add(datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc), 12000)
        body = self.client.get(self.url, params={"start_date": "2026-10-01T00:00:00"}).json()
        self.assertEqual([i["harga"] for i in body["items"]], [12000])
        self.assertEqual(body["total"], 1)

    def test_only_end_date_filters_instead_of_being_silently_ignored(self):
        self._add(datetime(2026, 9, 1, 5, 0, tzinfo=timezone.utc), 10000)
        self._add(datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc), 12000)
        body = self.client.get(self.url, params={"end_date": "2026-10-01T00:00:00"}).json()
        self.assertEqual([i["harga"] for i in body["items"]], [10000])

    def test_explicit_dates_take_priority_over_the_range_preset(self):
        self._add(datetime(2026, 9, 1, 5, 0, tzinfo=timezone.utc), 10000)
        body = self.client.get(self.url, params={"range": "1m", "start_date": "2026-08-01T00:00:00"}).json()
        self.assertEqual([i["harga"] for i in body["items"]], [10000])


if __name__ == "__main__":
    unittest.main()
