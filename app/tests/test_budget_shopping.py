import unittest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.models.db_models import Product, Store, PriceEntry
from app.models.schemas import VerificationStatus

SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class TestBudgetShoppingAssistant(unittest.TestCase):

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        self.original_get_db = app.dependency_overrides.get(get_db)
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)

        # Seed Stores (using valid UUID strings)
        self.store1_id = "a1111111-1111-1111-1111-111111111111"
        self.store2_id = "a2222222-2222-2222-2222-222222222222"
        self.user_id = "b1111111-1111-1111-1111-111111111111"
        
        # Seed Products (using valid UUID strings)
        self.prod1_id = "c1111111-1111-1111-1111-111111111111"
        self.prod2_id = "c2222222-2222-2222-2222-222222222222"

        store1 = Store(id=self.store1_id, nama="Indomaret Gatsu", lat=-6.2, lng=106.8)
        store2 = Store(id=self.store2_id, nama="Alfamart Sudirman", lat=-6.2, lng=106.8)
        
        prod1 = Product(id=self.prod1_id, nama="Susu UHT 1L", kategori="Susu & Olahan", ukuran=1000.0, satuan="ml")
        prod2 = Product(id=self.prod2_id, nama="Roti Tawar", kategori="Makanan Pokok", ukuran=1.0, satuan="pcs")

        self.db.add_all([store1, store2, prod1, prod2])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)
        if self.original_get_db is not None:
            app.dependency_overrides[get_db] = self.original_get_db
        else:
            app.dependency_overrides.pop(get_db, None)

    def test_budget_shopping_recommend_endpoint_empty_items(self):
        # Empty items payload should trigger 422 Unprocessable Entity
        payload = {
            "budget": 100000,
            "items": []
        }
        response = self.client.post("/budget-shopping/recommend", json=payload)
        self.assertEqual(response.status_code, 422)

    def test_verified_prices_are_used(self):
        # 1. Verified prices exist for both products at store1
        p1 = PriceEntry(product_id=self.prod1_id, store_id=self.store1_id, harga=20000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)
        p2 = PriceEntry(product_id=self.prod2_id, store_id=self.store1_id, harga=15000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)
        self.db.add_all([p1, p2])
        self.db.commit()

        payload = {
            "budget": 50000,
            "items": [
                {"product_id": self.prod1_id, "qty": 1},
                {"product_id": self.prod2_id, "qty": 1}
            ]
        }
        response = self.client.post("/budget-shopping/recommend", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertTrue(data["is_full_match"])
        self.assertEqual(data["recommended_store"]["store_id"], self.store1_id)
        self.assertEqual(data["total_cost"], 35000.0)

    def test_pending_and_rejected_prices_are_ignored(self):
        # 2 & 3. Store1 has verified prices (20k + 15k = 35k).
        # Store2 has cheaper pending & rejected prices (5k + 5k = 10k), but no verified prices.
        p1_v = PriceEntry(product_id=self.prod1_id, store_id=self.store1_id, harga=20000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)
        p2_v = PriceEntry(product_id=self.prod2_id, store_id=self.store1_id, harga=15000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)
        
        p1_p = PriceEntry(product_id=self.prod1_id, store_id=self.store2_id, harga=5000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.PENDING.value)
        p2_r = PriceEntry(product_id=self.prod2_id, store_id=self.store2_id, harga=5000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.REJECTED.value)

        self.db.add_all([p1_v, p2_v, p1_p, p2_r])
        self.db.commit()

        payload = {
            "budget": 50000,
            "items": [
                {"product_id": self.prod1_id, "qty": 1},
                {"product_id": self.prod2_id, "qty": 1}
            ]
        }
        response = self.client.post("/budget-shopping/recommend", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Store2 must NOT be chosen despite cheaper price because its prices are pending/rejected
        self.assertTrue(data["is_full_match"])
        self.assertEqual(data["recommended_store"]["store_id"], self.store1_id)
        self.assertEqual(data["total_cost"], 35000.0)

    def test_cheapest_pending_price_not_selected(self):
        # 4 & 5. At Store1, prod1 has a cheaper pending price (10k) and a verified price (25k).
        # Store1 also has verified prod2 (15k).
        # Total with verified = 25k + 15k = 40k.
        p1_pending = PriceEntry(product_id=self.prod1_id, store_id=self.store1_id, harga=10000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.PENDING.value)
        p1_verified = PriceEntry(product_id=self.prod1_id, store_id=self.store1_id, harga=25000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)
        p2_verified = PriceEntry(product_id=self.prod2_id, store_id=self.store1_id, harga=15000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)

        self.db.add_all([p1_pending, p1_verified, p2_verified])
        self.db.commit()

        payload = {
            "budget": 50000,
            "items": [
                {"product_id": self.prod1_id, "qty": 1},
                {"product_id": self.prod2_id, "qty": 1}
            ]
        }
        response = self.client.post("/budget-shopping/recommend", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertTrue(data["is_full_match"])
        self.assertEqual(data["total_cost"], 40000.0) # Uses 25k verified, NOT 10k pending

    def test_all_unverified_prices_yields_no_recommendation(self):
        # 6. If all prices are pending or rejected, Budget Shopping must NOT recommend any store
        p1_p = PriceEntry(product_id=self.prod1_id, store_id=self.store1_id, harga=10000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.PENDING.value)
        p2_r = PriceEntry(product_id=self.prod2_id, store_id=self.store1_id, harga=10000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.REJECTED.value)

        self.db.add_all([p1_p, p2_r])
        self.db.commit()

        payload = {
            "budget": 50000,
            "items": [
                {"product_id": self.prod1_id, "qty": 1},
                {"product_id": self.prod2_id, "qty": 1}
            ]
        }
        response = self.client.post("/budget-shopping/recommend", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertFalse(data["is_full_match"])
        self.assertIsNone(data["recommended_store"])
        self.assertEqual(data["items"], [])

if __name__ == "__main__":
    unittest.main()
