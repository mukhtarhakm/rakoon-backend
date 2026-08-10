import unittest
from datetime import datetime, timedelta
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


app.dependency_overrides[get_db] = override_get_db


class TestPriceHistoryBackend(unittest.TestCase):

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()

        # Seed test products
        product1 = Product(id=1, nama="Susu UHT 1L", kategori="Dairy", ukuran=1.0, satuan="L")
        product2 = Product(id=2, nama="Kopi Tubruk 200g", kategori="Beverages", ukuran=200.0, satuan="g")

        # Seed test stores
        store1 = Store(id="store_001", nama="Indomaret Sudirman")
        store2 = Store(id="store_002", nama="Alfamart Gatot Subroto")

        self.db.add_all([product1, product2, store1, store2])

        now = datetime.utcnow()
        day1 = now - timedelta(days=5)
        day2 = now - timedelta(days=2)
        day_old = now - timedelta(days=60)

        # Seed price entries
        p1 = PriceEntry(id=101, product_id=1, store_id="store_001", harga=18000, sumber_user_id="user1", timestamp=day1, status_verifikasi="verified")
        p2 = PriceEntry(id=102, product_id=1, store_id="store_001", harga=19000, sumber_user_id="user2", timestamp=day1, status_verifikasi="pending")
        p3 = PriceEntry(id=103, product_id=1, store_id="store_002", harga=20000, sumber_user_id="user1", timestamp=day2, status_verifikasi="verified")
        p_old = PriceEntry(id=104, product_id=1, store_id="store_001", harga=15000, sumber_user_id="user3", timestamp=day_old, status_verifikasi="verified")

        self.db.add_all([p1, p2, p3, p_old])
        self.db.commit()

        self.client = TestClient(app)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_service_get_price_history_normal(self):
        res = get_price_history(self.db, product_id=1)
        self.assertEqual(res.product_id, 1)
        self.assertEqual(res.total, 4)
        self.assertEqual(len(res.items), 4)
        self.assertGreater(len(res.trend), 0)

    def test_service_get_scan_price_history_entries(self):
        entries = get_scan_price_history_entries(self.db, product_id=1, store_id="store_001")
        self.assertEqual(len(entries), 3)
        for entry in entries:
            self.assertEqual(entry.store_id, "store_001")

    def test_endpoint_get_price_history_success(self):
        response = self.client.get("/api/v1/products/1/price-history")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["product_id"], 1)
        self.assertEqual(data["total"], 4)
        self.assertEqual(len(data["items"]), 4)
        self.assertEqual(len(data["trend"]), 3)

    def test_endpoint_get_price_history_filter_store(self):
        response = self.client.get("/api/v1/products/1/price-history?store_id=store_001")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 3)
        for item in data["items"]:
            self.assertEqual(item["store_id"], "store_001")

    def test_endpoint_get_price_history_filter_range(self):
        response = self.client.get("/api/v1/products/1/price-history?range=1m")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 3)

    def test_endpoint_get_price_history_empty(self):
        response = self.client.get("/api/v1/products/2/price-history")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["product_id"], 2)
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["items"], [])
        self.assertEqual(data["trend"], [])

    def test_endpoint_get_price_history_not_found(self):
        response = self.client.get("/api/v1/products/999/price-history")
        self.assertEqual(response.status_code, 404)
        data = response.json()
        self.assertEqual(data["detail"], "Produk dengan ID 999 tidak ditemukan.")


if __name__ == "__main__":
    unittest.main()
