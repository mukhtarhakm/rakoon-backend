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

    def test_overbudget_returns_cheapest_store_and_items(self):
        # Verified prices exist at store1 (20k + 15k = 35k) and store2 (15k + 14k = 29k).
        # Both are over budget of 20k. Recommendation should return store2 (cheapest over-budget).
        p1_s1 = PriceEntry(product_id=self.prod1_id, store_id=self.store1_id, harga=20000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)
        p2_s1 = PriceEntry(product_id=self.prod2_id, store_id=self.store1_id, harga=15000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)
        
        p1_s2 = PriceEntry(product_id=self.prod1_id, store_id=self.store2_id, harga=15000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)
        p2_s2 = PriceEntry(product_id=self.prod2_id, store_id=self.store2_id, harga=14000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)

        self.db.add_all([p1_s1, p2_s1, p1_s2, p2_s2])
        self.db.commit()

        payload = {
            "budget": 20000,
            "items": [
                {"product_id": self.prod1_id, "qty": 1},
                {"product_id": self.prod2_id, "qty": 1}
            ]
        }
        response = self.client.post("/budget-shopping/recommend", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertTrue(data["is_full_match"])
        self.assertEqual(data["recommended_store"]["store_id"], self.store2_id)
        self.assertEqual(data["total_cost"], 29000.0)
        self.assertEqual(data["remaining_budget"], -9000.0)
        self.assertEqual(len(data["items"]), 2)

    def test_no_full_match_returns_availabilities(self):
        # Product 1 is available at Store 1 (15k). Product 2 has no verified prices anywhere.
        p1 = PriceEntry(product_id=self.prod1_id, store_id=self.store1_id, harga=15000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)
        self.db.add(p1)
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
        self.assertEqual(len(data["product_availabilities"]), 2)

        p1_avail = next(x for x in data["product_availabilities"] if x["product_id"] == self.prod1_id)
        self.assertTrue(p1_avail["is_available"])
        self.assertEqual(p1_avail["harga_terendah"], 15000.0)
        self.assertEqual(p1_avail["toko_terendah"], "Indomaret Gatsu")

        p2_avail = next(x for x in data["product_availabilities"] if x["product_id"] == self.prod2_id)
        self.assertFalse(p2_avail["is_available"])
        self.assertIsNone(p2_avail["harga_terendah"])
        self.assertIsNone(p2_avail["toko_terendah"])

    def test_cheapest_full_match_store_is_selected_and_alternatives_sorted(self):
        # Seed prices at Store 1 (cheapest: 10k + 10k = 20k) and Store 2 (expensive: 15k + 15k = 30k)
        # Seed prices at Store 3 (middle: 12k + 12k = 24k)
        # Store 3 is created dynamically in db. Let's create store3 in setUp or dynamically:
        store3 = Store(nama="Karya Agung", alamat="Jl. Tengah", lat=-6.2300, lng=106.8400)
        self.db.add(store3)
        self.db.commit()

        p1_s1 = PriceEntry(product_id=self.prod1_id, store_id=self.store1_id, harga=10000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)
        p2_s1 = PriceEntry(product_id=self.prod2_id, store_id=self.store1_id, harga=10000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)

        p1_s2 = PriceEntry(product_id=self.prod1_id, store_id=self.store2_id, harga=15000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)
        p2_s2 = PriceEntry(product_id=self.prod2_id, store_id=self.store2_id, harga=15000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)

        p1_s3 = PriceEntry(product_id=self.prod1_id, store_id=store3.id, harga=12000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)
        p2_s3 = PriceEntry(product_id=self.prod2_id, store_id=store3.id, harga=12000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)

        self.db.add_all([p1_s1, p2_s1, p1_s2, p2_s2, p1_s3, p2_s3])
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

        # Store 1 (20k) is cheapest, so it MUST be recommended (instead of Store 2 or 3)
        self.assertEqual(data["recommended_store"]["store_id"], self.store1_id)
        self.assertEqual(data["total_cost"], 20000.0)

        # Alternatives should contain Store 3 (24k) and Store 2 (30k) in sorted order
        self.assertEqual(len(data["store_alternatives"]), 2)
        self.assertEqual(data["store_alternatives"][0]["store_info"]["nama"], "Karya Agung") # Store 3
        self.assertEqual(data["store_alternatives"][0]["total_cost"], 24000.0)
        self.assertEqual(data["store_alternatives"][1]["store_info"]["nama"], "Alfamart Sudirman") # Store 2
        self.assertEqual(data["store_alternatives"][1]["total_cost"], 30000.0)

        # Primary store must NOT be duplicated in alternatives
        self.assertNotIn(self.store1_id, [a["store_info"]["store_id"] for a in data["store_alternatives"]])

    def test_quantity_greater_than_one_calculates_correct_totals(self):
        # Qty = 2. Store 1 price = 10k (total 20k). Store 2 price = 12k (total 24k).
        # We also have Roti at qty = 1. Store 1 = 15k (total 35k). Store 2 = 13k (total 37k).
        p1_s1 = PriceEntry(product_id=self.prod1_id, store_id=self.store1_id, harga=10000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)
        p2_s1 = PriceEntry(product_id=self.prod2_id, store_id=self.store1_id, harga=15000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)

        p1_s2 = PriceEntry(product_id=self.prod1_id, store_id=self.store2_id, harga=12000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)
        p2_s2 = PriceEntry(product_id=self.prod2_id, store_id=self.store2_id, harga=13000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)

        self.db.add_all([p1_s1, p2_s1, p1_s2, p2_s2])
        self.db.commit()

        payload = {
            "budget": 50000,
            "items": [
                {"product_id": self.prod1_id, "qty": 2},
                {"product_id": self.prod2_id, "qty": 1}
            ]
        }
        response = self.client.post("/budget-shopping/recommend", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Store 1 should be recommended: (2 * 10k) + (1 * 15k) = 35k
        self.assertEqual(data["recommended_store"]["store_id"], self.store1_id)
        self.assertEqual(data["total_cost"], 35000.0)
        self.assertEqual(data["remaining_budget"], 15000.0)

        # Store 2 is alternative: (2 * 12k) + (1 * 13k) = 37k
        self.assertEqual(len(data["store_alternatives"]), 1)
        self.assertEqual(data["store_alternatives"][0]["store_info"]["store_id"], self.store2_id)
        self.assertEqual(data["store_alternatives"][0]["total_cost"], 37000.0)

    def test_equal_cost_has_deterministic_alphabetical_tie_breaker(self):
        # Seed prices at Store 1 ("Indomaret Gatsu") and Store 2 ("Alfamart Gatsu").
        # Both costs are identical (20k).
        # Store 2 ("Alfamart Gatsu") must be selected as primary due to alphabetical ordering.
        p1_s1 = PriceEntry(product_id=self.prod1_id, store_id=self.store1_id, harga=10000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)
        p2_s1 = PriceEntry(product_id=self.prod2_id, store_id=self.store1_id, harga=10000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)

        p1_s2 = PriceEntry(product_id=self.prod1_id, store_id=self.store2_id, harga=10000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)
        p2_s2 = PriceEntry(product_id=self.prod2_id, store_id=self.store2_id, harga=10000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED.value)

        self.db.add_all([p1_s1, p2_s1, p1_s2, p2_s2])
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

        # Alfamart Sudirman comes first alphabetically compared to Indomaret Gatsu
        self.assertEqual(data["recommended_store"]["nama"], "Alfamart Sudirman")
        self.assertEqual(data["store_alternatives"][0]["store_info"]["nama"], "Indomaret Gatsu")

if __name__ == "__main__":
    unittest.main()
