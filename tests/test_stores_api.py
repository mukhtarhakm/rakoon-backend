import unittest
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.models.db_models import Product, Store, PriceEntry

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


class TestStoresEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()

    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()

        # Seed stores with valid hex UUIDs
        self.store1_id = "11111111-1111-4111-8111-111111111111"
        self.store2_id = "22222222-2222-4222-8222-222222222222"
        self.store_far_id = "33333333-3333-4333-8333-333333333333"

        self.store1 = Store(
            id=self.store1_id,
            nama="Super Indo Kaliurang",
            alamat="Jl. Kaliurang Km 5",
            lat=-7.7600,
            lng=110.3800,
        )
        self.store2 = Store(
            id=self.store2_id,
            nama="Indomaret Point Gejayan",
            alamat="Jl. Gejayan No 10",
            lat=-7.7620,
            lng=110.3820,
        )
        self.store_far = Store(
            id=self.store_far_id,
            nama="Toko Jauh Banget",
            alamat="Jl. Pantai Parangtritis",
            lat=-8.0000,
            lng=110.3000,
        )
        self.db.add_all([self.store1, self.store2, self.store_far])

        # Seed products
        self.prod1_id = "aaaaaaaa-1111-4111-8111-111111111111"
        self.prod2_id = "bbbbbbbb-2222-4222-8222-222222222222"
        self.prod3_id = "cccccccc-3333-4333-8333-333333333333"

        self.prod1 = Product(id=self.prod1_id, nama="Minyak Goreng 2L")
        self.prod2 = Product(id=self.prod2_id, nama="Beras 5kg")
        self.prod3 = Product(id=self.prod3_id, nama="Gula Pasir 1kg")
        self.db.add_all([self.prod1, self.prod2, self.prod3])

        # Seed price entries for store1: 2 valid products, 1 rejected product
        now = datetime.now(timezone.utc)
        self.pe1 = PriceEntry(
            id="44444444-1111-4111-8111-111111111111",
            product_id=self.prod1_id,
            store_id=self.store1_id,
            harga=32000,
            sumber_user_id="55555555-1111-4111-8111-111111111111",
            status_verifikasi="verified",
            timestamp=now,
        )
        self.pe2 = PriceEntry(
            id="66666666-2222-4222-8222-222222222222",
            product_id=self.prod2_id,
            store_id=self.store1_id,
            harga=65000,
            sumber_user_id="55555555-1111-4111-8111-111111111111",
            status_verifikasi="pending",
            timestamp=now,
        )
        self.pe3_rejected = PriceEntry(
            id="77777777-3333-4333-8333-333333333333",
            product_id=self.prod3_id,
            store_id=self.store1_id,
            harga=15000,
            sumber_user_id="55555555-1111-4111-8111-111111111111",
            status_verifikasi="rejected",
            timestamp=now,
        )
        self.db.add_all([self.pe1, self.pe2, self.pe3_rejected])
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_get_nearby_stores_includes_total_products(self):
        # User location close to store1 and store2
        response = self.client.get("/stores/nearby?lat=-7.7600&lng=110.3800&radius_km=5.0")
        self.assertEqual(response.status_code, 200)

        data = response.json()
        self.assertIn("stores", data)
        stores = data["stores"]
        self.assertEqual(len(stores), 2)

        # store1 has 2 valid products (1 rejected is excluded)
        s1 = next((s for s in stores if s["store_id"] == self.store1.id), None)
        self.assertIsNotNone(s1)
        self.assertEqual(s1["nama"], "Super Indo Kaliurang")
        self.assertEqual(s1["total_products"], 2)

        # store2 has 0 recorded products
        s2 = next((s for s in stores if s["store_id"] == self.store2.id), None)
        self.assertIsNotNone(s2)
        self.assertEqual(s2["nama"], "Indomaret Point Gejayan")
        self.assertEqual(s2["total_products"], 0)

    def test_get_store_products(self):
        response = self.client.get(f"/stores/{self.store1.id}/products")
        self.assertEqual(response.status_code, 200)

        products = response.json()
        # Only non-rejected products should be returned (prod1 and prod2)
        self.assertEqual(len(products), 2)
        prod_names = [p["nama"] for p in products]
        self.assertIn("Minyak Goreng 2L", prod_names)
        self.assertIn("Beras 5kg", prod_names)
        self.assertNotIn("Gula Pasir 1kg", prod_names)


if __name__ == "__main__":
    unittest.main()
