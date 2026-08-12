import unittest
from fastapi.testclient import TestClient
from app.main import app
from app.models.schemas import RecommendationCandidate
from app.routers.recommendation import (
    normalize_unit_and_dimension,
    evaluate_best_value_logic,
)

class TestRecommendationFeature(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(app)

    def test_unit_normalization(self):
        # Test volume
        norm = normalize_unit_and_dimension("ml", 500)
        self.assertEqual(norm, ("volume", "ml", 500.0))

        norm_l = normalize_unit_and_dimension("liter", 1.5)
        self.assertEqual(norm_l, ("volume", "ml", 1500.0))

        # Test weight
        norm_g = normalize_unit_and_dimension("gr", 250)
        self.assertEqual(norm_g, ("weight", "g", 250.0))

        norm_kg = normalize_unit_and_dimension("kg", 2.0)
        self.assertEqual(norm_kg, ("weight", "g", 2000.0))

        # Test count
        norm_pcs = normalize_unit_and_dimension("pcs", 10)
        self.assertEqual(norm_pcs, ("count", "pcs", 10.0))

        # Test unknown unit
        self.assertIsNone(normalize_unit_and_dimension("unknown_unit", 10))
        self.assertIsNone(normalize_unit_and_dimension(None, 10))

    def test_best_value_calculation_ranking(self):
        # Scenario:
        # Susu A: Rp20.000 / 200 ml = Rp100.0/ml
        # Susu B: Rp25.000 / 350 ml = Rp71.43/ml
        # Susu C: Rp30.000 / 0.5 liter (500 ml) = Rp60.0/ml -> Best Value!
        items = [
            RecommendationCandidate(product_id="a", nama_produk="Susu A", harga=20000, ukuran=200, satuan="ml"),
            RecommendationCandidate(product_id="b", nama_produk="Susu B", harga=25000, ukuran=350, satuan="ml"),
            RecommendationCandidate(product_id="c", nama_produk="Susu C", harga=30000, ukuran=0.5, satuan="liter"),
        ]

        res = evaluate_best_value_logic(items)

        self.assertEqual(res.total_evaluated, 3)
        self.assertEqual(res.total_valid, 3)
        self.assertEqual(res.total_excluded, 0)
        self.assertIsNotNone(res.best_value)
        self.assertEqual(res.best_value.product_id, "c")
        self.assertEqual(res.best_value.nama_produk, "Susu C")
        self.assertEqual(res.best_value.rank, 1)
        self.assertTrue(res.best_value.is_best_value)
        self.assertEqual(res.best_value.badge, "BEST VALUE")
        self.assertEqual(res.best_value.harga_per_unit, 60.0)

        # Verify ranked items order
        self.assertEqual(res.ranked_items[0].product_id, "c")
        self.assertEqual(res.ranked_items[1].product_id, "b")
        self.assertEqual(res.ranked_items[2].product_id, "a")

    def test_invalid_items_exclusion(self):
        items = [
            RecommendationCandidate(product_id="valid", nama_produk="Susu Valid", harga=15000, ukuran=250, satuan="ml"),
            RecommendationCandidate(product_id="no_price", nama_produk="Susu Free", harga=0, ukuran=250, satuan="ml"),
            RecommendationCandidate(product_id="no_size", nama_produk="Susu Micro", harga=10000, ukuran=0, satuan="ml"),
            RecommendationCandidate(product_id="bad_unit", nama_produk="Susu Kotak", harga=10000, ukuran=1, satuan="box"),
            RecommendationCandidate(product_id="no_name", nama_produk="", harga=10000, ukuran=200, satuan="ml"),
        ]

        res = evaluate_best_value_logic(items)

        self.assertEqual(res.total_evaluated, 5)
        self.assertEqual(res.total_valid, 1)
        self.assertEqual(res.total_excluded, 4)
        self.assertEqual(res.best_value.product_id, "valid")
        self.assertEqual(len(res.excluded_items), 4)

    def test_recommendation_api_endpoint(self):
        payload = {
            "category": "Susu UHT",
            "items": [
                {"product_id": "p1", "nama_produk": "Susu A", "harga": 20000, "ukuran": 200, "satuan": "ml"},
                {"product_id": "p2", "nama_produk": "Susu B", "harga": 25000, "ukuran": 350, "satuan": "ml"},
                {"product_id": "p3", "nama_produk": "Susu C", "harga": 30000, "ukuran": 500, "satuan": "ml"},
                {"product_id": "p4", "nama_produk": "Susu Invalid", "harga": -5000, "ukuran": 200, "satuan": "ml"}
            ]
        }

        response = self.client.post("/recommendation/evaluate", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertEqual(data["total_evaluated"], 4)
        self.assertEqual(data["total_valid"], 3)
        self.assertEqual(data["total_excluded"], 1)
        self.assertEqual(data["best_value"]["product_id"], "p3")
        self.assertTrue(data["best_value"]["is_best_value"])
        self.assertEqual(data["best_value"]["badge"], "BEST VALUE")

if __name__ == "__main__":
    unittest.main()
