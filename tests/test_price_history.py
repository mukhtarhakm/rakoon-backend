import unittest
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.models.db_models import Product, Store, PriceEntry
from app.services.price_history_service import (
    get_price_history,
    get_scan_price_history_entries,
    calculate_price_trend,
    DateRange,
)

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


class TestPriceHistoryBackend(unittest.TestCase):

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db


        self.prod_id1 = "d1111111-1111-1111-1111-111111111111"
        self.prod_id2 = "d2222222-2222-2222-2222-222222222222"
        self.store_id1 = "a5b07384-d113-4956-b51c-43f11075d654"
        self.store_id2 = "a5b07384-d113-4956-b51c-43f11075d655"
        self.user_id = "b5b07384-d113-4956-b51c-43f11075d655"

        # Seed test products
        product1 = Product(id=self.prod_id1, nama="Susu UHT 1L", kategori="Dairy", ukuran=1.0, satuan="L")
        product2 = Product(id=self.prod_id2, nama="Kopi Tubruk 200g", kategori="Beverages", ukuran=200.0, satuan="g")

        # Seed test stores
        store1 = Store(id=self.store_id1, nama="Indomaret Sudirman")
        store2 = Store(id=self.store_id2, nama="Alfamart Gatot Subroto")

        self.db.add_all([product1, product2, store1, store2])

        now = datetime.now(timezone.utc)
        day1 = now - timedelta(days=5)
        day2 = now - timedelta(days=2)
        day_old = now - timedelta(days=60)

        # Seed price entries
        p1 = PriceEntry(id="e1111111-1111-1111-1111-111111111111", product_id=self.prod_id1, store_id=self.store_id1, harga=18000, sumber_user_id=self.user_id, timestamp=day1, status_verifikasi="verified")
        p2 = PriceEntry(id="e2222222-2222-2222-2222-222222222222", product_id=self.prod_id1, store_id=self.store_id1, harga=19000, sumber_user_id=self.user_id, timestamp=day1, status_verifikasi="pending")
        p3 = PriceEntry(id="e3333333-3333-3333-3333-333333333333", product_id=self.prod_id1, store_id=self.store_id2, harga=20000, sumber_user_id=self.user_id, timestamp=day2, status_verifikasi="verified")
        p_old = PriceEntry(id="e4444444-4444-4444-4444-444444444444", product_id=self.prod_id1, store_id=self.store_id1, harga=15000, sumber_user_id=self.user_id, timestamp=day_old, status_verifikasi="verified")

        self.db.add_all([p1, p2, p3, p_old])
        self.db.commit()

        self.client = TestClient(app)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)
        app.dependency_overrides.clear()


    def test_service_get_price_history_normal(self):
        res = get_price_history(self.db, product_id=self.prod_id1)
        self.assertEqual(str(res.product_id), self.prod_id1)
        self.assertEqual(res.product_name, "Susu UHT 1L")
        self.assertEqual(res.total, 4)
        self.assertEqual(len(res.items), 4)
        self.assertGreater(len(res.trend), 0)
        for item in res.items:
            if str(item.store_id) == self.store_id1:
                self.assertEqual(item.store_name, "Indomaret Sudirman")
            elif str(item.store_id) == self.store_id2:
                self.assertEqual(item.store_name, "Alfamart Gatot Subroto")

    def test_service_get_scan_price_history_entries(self):
        entries = get_scan_price_history_entries(self.db, product_id=self.prod_id1, store_id=self.store_id1)
        self.assertEqual(len(entries), 3)
        for entry in entries:
            self.assertEqual(str(entry.store_id), self.store_id1)
            self.assertEqual(entry.store_name, "Indomaret Sudirman")

    def test_endpoint_get_price_history_success(self):
        response = self.client.get(f"/price/api/v1/products/{self.prod_id1}/price-history")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(str(data["product_id"]), self.prod_id1)
        self.assertEqual(data["product_name"], "Susu UHT 1L")
        self.assertEqual(data["total"], 4)
        self.assertEqual(len(data["items"]), 4)
        self.assertEqual(len(data["trend"]), 3)
        for item in data["items"]:
            if str(item["store_id"]) == self.store_id1:
                self.assertEqual(item["store_name"], "Indomaret Sudirman")
            elif str(item["store_id"]) == self.store_id2:
                self.assertEqual(item["store_name"], "Alfamart Gatot Subroto")
        for item in data["items"]:
            self.assertNotIn("sumber_user_id", item)

    def test_endpoint_get_legacy_price_history_excludes_sumber_user_id(self):
        response = self.client.get(f"/price/product/{self.prod_id1}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)
        for item in data:
            self.assertNotIn("sumber_user_id", item)

    def test_endpoint_get_price_history_filter_store(self):
        response = self.client.get(f"/price/api/v1/products/{self.prod_id1}/price-history?store_id={self.store_id1}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 3)
        for item in data["items"]:
            self.assertEqual(str(item["store_id"]), self.store_id1)

    def test_endpoint_get_price_history_filter_range(self):
        response = self.client.get(f"/price/api/v1/products/{self.prod_id1}/price-history?range=1m")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 3)

    def test_endpoint_get_price_history_empty(self):
        response = self.client.get(f"/price/api/v1/products/{self.prod_id2}/price-history")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(str(data["product_id"]), self.prod_id2)
        self.assertEqual(data["product_name"], "Kopi Tubruk 200g")
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["items"], [])
        self.assertEqual(data["trend"], [])

    def test_endpoint_get_price_history_not_found(self):
        non_existent_uuid = "d9999999-9999-9999-9999-999999999999"
        response = self.client.get(f"/price/api/v1/products/{non_existent_uuid}/price-history")
        self.assertEqual(response.status_code, 404)
        data = response.json()
        self.assertEqual(data["detail"], f"Produk dengan ID {non_existent_uuid} tidak ditemukan.")


if __name__ == "__main__":
    unittest.main()
