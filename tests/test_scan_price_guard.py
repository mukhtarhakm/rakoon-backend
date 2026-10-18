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
from app.models.db_models import PriceEntry, Product, Store
from app.models.schemas import MAX_PRICE_RUPIAH, VerificationStatus
from app.routers.scan import (
    DAILY_CONFIRM_ITEM_QUOTA,
    PRICE_BAND_HIGH_FACTOR,
    PRICE_BAND_LOW_FACTOR,
    PRICE_BAND_MIN_SAMPLES,
)
from app.services.vision_service import parse_raw_items

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
SEED_USER = "33333333-3333-3333-3333-333333333333"


class TestConfirmPriceBand(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        self.current_user = USER_A
        app.dependency_overrides[get_current_user] = lambda: self.current_user
        self.client = TestClient(app)
        self.store = Store(nama="Toko Uji", lat=-7.75, lng=110.4)
        self.db.add(self.store)
        self.db.commit()
        self.store_id = str(self.store.id)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _seed_product(self, nama="Beras Uji 5kg", prices=(10000, 11000, 12000), status=VerificationStatus.VERIFIED):
        product = self.db.query(Product).filter(Product.nama == nama).first()
        if not product:
            product = Product(nama=nama, kategori="Makanan Pokok", ukuran=5.0, satuan="kg")
            self.db.add(product)
            self.db.commit()
        for harga in prices:
            self.db.add(PriceEntry(
                product_id=product.id, store_id=self.store_id, harga=harga,
                sumber_user_id=SEED_USER, status_verifikasi=status,
            ))
        self.db.commit()
        return product

    def _confirm(self, nama, harga, user=None):
        if user:
            self.current_user = user
        return self.client.post("/scan/confirm", json={
            "store_id": self.store_id,
            "user_id": "diabaikan",
            "items": [{"nama_produk": nama, "harga": harga, "ukuran": 5.0, "satuan": "kg", "kategori": "Makanan Pokok"}],
        })

    def _last_status(self, product, harga=None):
        """Status entri terakhir milik pengguna; `harga` memilih entri persis (timestamp bisa kembar di Windows)."""
        self.db.expire_all()
        query = self.db.query(PriceEntry).filter(
            PriceEntry.product_id == product.id, PriceEntry.sumber_user_id == self.current_user
        )
        if harga is not None:
            query = query.filter(PriceEntry.harga == harga)
        return query.order_by(PriceEntry.timestamp.desc()).first().status_verifikasi

    def test_price_inside_the_band_is_verified(self):
        product = self._seed_product()
        res = self._confirm(product.nama, 12500)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(self._last_status(product), VerificationStatus.VERIFIED)
        self.assertEqual(res.json()["items_pending_review"], 0)

    def test_price_outside_the_band_is_pending_with_a_notice(self):
        product = self._seed_product()  # median 11000
        for harga in (1000, 3299, 33001, 500000):
            with self.subTest(harga=harga):
                res = self._confirm(product.nama, harga)
                self.assertEqual(res.status_code, 201)
                self.assertEqual(self._last_status(product), VerificationStatus.PENDING)
                self.assertEqual(res.json()["items_saved"], 1)
                self.assertEqual(res.json()["items_pending_review"], 1)
                self.assertIn("verifikasi", res.json()["message"].lower())

    def test_band_edges_are_inclusive(self):
        median = 11000
        low, high = int(median * PRICE_BAND_LOW_FACTOR), int(median * PRICE_BAND_HIGH_FACTOR)
        for i, harga in enumerate((low, high)):
            with self.subTest(harga=harga):
                product = self._seed_product(nama=f"Beras Tepi {i}")  # produk terpisah: konfirmasi pertama menggeser median
                self._confirm(product.nama, harga)
                self.assertEqual(self._last_status(product), VerificationStatus.VERIFIED)

    def test_pending_and_rejected_entries_do_not_move_the_median(self):
        product = self._seed_product(prices=(10000, 10000, 10000))
        self._seed_product(prices=(1, 1, 1, 1, 1), status=VerificationStatus.PENDING)
        self._seed_product(prices=(1, 1, 1), status=VerificationStatus.REJECTED)
        self._confirm(product.nama, 9000)
        self.assertEqual(self._last_status(product, 9000), VerificationStatus.VERIFIED)
        # suntikan murah berulang tidak menggeser median sehingga tetap pending
        for harga in (2000, 2001, 2002):  # harga berbeda: kiriman identik dalam 10 menit dilewati
            self._confirm(product.nama, harga)
        self.db.expire_all()
        injected = self.db.query(PriceEntry).filter(PriceEntry.harga.in_((2000, 2001, 2002))).all()
        self.assertEqual(len(injected), 3)
        self.assertEqual({e.status_verifikasi for e in injected}, {VerificationStatus.PENDING})

    def test_without_enough_verified_samples_the_price_stays_verified(self):
        # Batas yang diketahui: produk baru/sedikit data tidak punya median yang bisa dipercaya.
        samples = PRICE_BAND_MIN_SAMPLES - 1
        product = self._seed_product(prices=[10000] * samples)
        self._confirm(product.nama, 900000)
        self.assertEqual(self._last_status(product), VerificationStatus.VERIFIED)

        res = self._confirm("Produk Baru Sekali", 5000)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["items_pending_review"], 0)

    def test_daily_quota_blocks_the_whole_request_and_is_per_user(self):
        product = self._seed_product()
        with patch("app.routers.scan.DAILY_CONFIRM_ITEM_QUOTA", 5):
            prices = iter(range(11000, 12000))  # harga unik per item: entri kembar dilewati dan tidak memakai kuota

            def confirm_many(n, user):
                self.current_user = user
                return self.client.post("/scan/confirm", json={
                    "store_id": self.store_id, "user_id": "x",
                    "items": [{"nama_produk": product.nama, "harga": next(prices), "kategori": "Makanan Pokok"} for _ in range(n)],
                })

            self.assertEqual(confirm_many(3, USER_A).status_code, 201)
            before = self.db.query(PriceEntry).count()
            blocked = confirm_many(3, USER_A)  # 3 + 3 > 5
            self.assertEqual(blocked.status_code, 429)
            self.assertIn("kuota", blocked.json()["detail"].lower())
            self.db.expire_all()
            self.assertEqual(self.db.query(PriceEntry).count(), before)  # tidak ada yang tersimpan sebagian

            self.assertEqual(confirm_many(2, USER_A).status_code, 201)  # tepat mencapai batas
            self.assertEqual(confirm_many(1, USER_A).status_code, 429)
            self.assertEqual(confirm_many(5, USER_B).status_code, 201)  # pengguna lain tidak terpengaruh

    def test_entries_from_before_today_do_not_count_toward_the_daily_quota(self):
        product = self._seed_product()
        yesterday = datetime.now(timezone.utc) - timedelta(days=1, minutes=1)
        for _ in range(5):
            self.db.add(PriceEntry(
                product_id=product.id, store_id=self.store_id, harga=11000,
                sumber_user_id=USER_A, status_verifikasi=VerificationStatus.VERIFIED, timestamp=yesterday,
            ))
        self.db.commit()
        with patch("app.routers.scan.DAILY_CONFIRM_ITEM_QUOTA", 5):
            self.assertEqual(self._confirm(product.nama, 11000).status_code, 201)

    def test_default_daily_quota_allows_at_least_one_full_batch(self):
        self.assertGreaterEqual(DAILY_CONFIRM_ITEM_QUOTA, 100)


class TestScanResultPriceSanity(unittest.TestCase):
    def _parse(self, harga):
        return parse_raw_items([
            {"nama_produk": "Permen", "harga": harga, "ukuran": 10, "satuan": "gr", "confidence": "tinggi"}
        ])[0]

    def test_zero_negative_and_extreme_prices_need_verification_even_with_high_confidence(self):
        for harga in (0, -1000, MAX_PRICE_RUPIAH + 1, 50000000):
            with self.subTest(harga=harga):
                self.assertTrue(self._parse(harga).needs_verification)

    def test_reasonable_price_with_high_confidence_does_not_need_verification(self):
        for harga in (1, 2500, MAX_PRICE_RUPIAH):
            with self.subTest(harga=harga):
                self.assertFalse(self._parse(harga).needs_verification)


if __name__ == "__main__":
    unittest.main()
