import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.db_models import PriceEntry, Product, Store
from app.models.schemas import VerificationStatus
from app.services.current_price import (
    STALE_AFTER_DAYS,
    CurrentPrice,
    get_current_prices,
    pick_cheapest,
)

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

USER = "11111111-1111-1111-1111-111111111111"
V, P, R = VerificationStatus.VERIFIED, VerificationStatus.PENDING, VerificationStatus.REJECTED


class TestCurrentPriceService(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        self.now = datetime.now(timezone.utc)
        self.store_a = Store(nama="Toko A", lat=-7.78, lng=110.4)
        self.store_b = Store(nama="Toko B", lat=-7.79, lng=110.41)
        self.product = Product(nama="Susu Uji 1L", kategori="Minuman", ukuran=1.0, satuan="l")
        self.db.add_all([self.store_a, self.store_b, self.product])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _add(self, store, harga, days_ago, status=V, product=None):
        self.db.add(PriceEntry(
            product_id=(product or self.product).id,
            store_id=store.id,
            harga=harga,
            sumber_user_id=USER,
            status_verifikasi=status,
            timestamp=self.now - timedelta(days=days_ago),
        ))
        self.db.commit()

    def _current(self, **kwargs):
        return get_current_prices(self.db, **kwargs)

    def test_takes_the_latest_verified_price_per_product_and_store(self):
        self._add(self.store_a, 9000, 10)
        self._add(self.store_a, 12000, 2)
        self._add(self.store_b, 15000, 3)
        prices = self._current()
        self.assertEqual(prices[(str(self.product.id), str(self.store_a.id))].harga, 12000)
        self.assertEqual(prices[(str(self.product.id), str(self.store_b.id))].harga, 15000)
        self.assertEqual(len(prices), 2)

    def test_pending_and_rejected_entries_are_never_current(self):
        self._add(self.store_a, 12000, 5)
        self._add(self.store_a, 1000, 0, status=P)
        self._add(self.store_a, 500, 0, status=R)
        self.assertEqual(self._current()[(str(self.product.id), str(self.store_a.id))].harga, 12000)

    def test_product_with_only_pending_prices_has_no_current_price(self):
        self._add(self.store_a, 1000, 1, status=P)
        self.assertEqual(self._current(), {})

    def test_stale_only_after_the_named_threshold(self):
        self.assertEqual(STALE_AFTER_DAYS, 90)
        for days, expected in ((0, False), (89, False), (90, False), (91, True), (400, True)):
            with self.subTest(days_ago=days):
                self.db.query(PriceEntry).delete()
                self.db.commit()
                self._add(self.store_a, 12000, days)
                price = self._current()[(str(self.product.id), str(self.store_a.id))]
                self.assertEqual(price.stale, expected)

    def test_old_only_price_is_still_returned_never_hidden(self):
        self._add(self.store_a, 70000, 400)
        price = self._current()[(str(self.product.id), str(self.store_a.id))]
        self.assertEqual(price.harga, 70000)
        self.assertTrue(price.stale)
        self.assertIsNotNone(price.timestamp.tzinfo)

    def test_filters_by_product_and_store_ids(self):
        other = Product(nama="Beras Uji 5kg", kategori="Makanan Pokok", ukuran=5.0, satuan="kg")
        self.db.add(other)
        self.db.commit()
        self._add(self.store_a, 12000, 1)
        self._add(self.store_b, 15000, 1)
        self._add(self.store_a, 70000, 1, product=other)

        only_other = self._current(product_ids=[str(other.id)])
        self.assertEqual({k[0] for k in only_other}, {str(other.id)})
        only_b = self._current(product_ids=[str(self.product.id)], store_ids=[str(self.store_b.id)])
        self.assertEqual(list(only_b), [(str(self.product.id), str(self.store_b.id))])
        self.assertEqual(self._current(product_ids=[]), {})

    def test_identical_timestamps_resolve_deterministically(self):
        ts_days = 3
        self._add(self.store_a, 12000, ts_days)
        entry = self.db.query(PriceEntry).first()
        twin = PriceEntry(
            product_id=entry.product_id, store_id=entry.store_id, harga=13000,
            sumber_user_id=USER, status_verifikasi=V, timestamp=entry.timestamp,
        )
        self.db.add(twin)
        self.db.commit()
        results = {self._current()[(str(self.product.id), str(self.store_a.id))].harga for _ in range(5)}
        self.assertEqual(len(results), 1)

    def test_pick_cheapest_prefers_fresh_over_a_cheaper_stale_price(self):
        fresh = CurrentPrice("p", "s1", 15000, self.now, False)
        stale_cheap = CurrentPrice("p", "s2", 9000, self.now - timedelta(days=200), True)
        self.assertIs(pick_cheapest([stale_cheap, fresh]), fresh)

    def test_pick_cheapest_falls_back_to_stale_when_nothing_is_fresh(self):
        a = CurrentPrice("p", "s1", 15000, self.now - timedelta(days=100), True)
        b = CurrentPrice("p", "s2", 9000, self.now - timedelta(days=300), True)
        self.assertIs(pick_cheapest([a, b]), b)
        self.assertIsNone(pick_cheapest([]))

    def test_pick_cheapest_breaks_ties_by_store_id(self):
        a = CurrentPrice("p", "s-b", 9000, self.now, False)
        b = CurrentPrice("p", "s-a", 9000, self.now, False)
        self.assertIs(pick_cheapest([a, b]), b)


if __name__ == "__main__":
    unittest.main()
