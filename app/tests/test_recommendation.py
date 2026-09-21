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

    def test_same_category_compatible_units_ranking(self):
        # Test Case 1: Two products in same category and unit compatible -> ranking succeeds
        items = [
            RecommendationCandidate(product_id="a", nama_produk="Ultra Milk 1L", harga=20000, ukuran=1000, satuan="ml", kategori="Susu & Olahan"),
            RecommendationCandidate(product_id="b", nama_produk="Indomilk 1L", harga=18000, ukuran=1000, satuan="ml", kategori="Susu & Olahan"),
        ]

        res = evaluate_best_value_logic(items)

        self.assertEqual(res.total_evaluated, 2)
        self.assertEqual(res.total_valid, 2)
        self.assertEqual(res.total_excluded, 0)
        self.assertEqual(len(res.categories), 1)

        cat_susu = res.categories[0]
        self.assertEqual(cat_susu.kategori, "Susu & Olahan")
        self.assertEqual(len(cat_susu.dimension_groups), 1)

        dim_vol = cat_susu.dimension_groups[0]
        self.assertTrue(dim_vol.is_comparable)
        self.assertIsNotNone(dim_vol.best_value)
        self.assertEqual(dim_vol.best_value.product_id, "b")
        self.assertEqual(dim_vol.best_value.nama_produk, "Indomilk 1L")
        self.assertTrue(dim_vol.best_value.is_best_value)
        self.assertEqual(dim_vol.best_value.badge, "BEST VALUE")
        self.assertEqual(dim_vol.ranked_items[0].product_id, "b")
        self.assertEqual(dim_vol.ranked_items[1].product_id, "a")

    def test_different_categories_separated(self):
        # Test Case 2 & 5: Products from different categories are separated and not compared together
        items = [
            RecommendationCandidate(product_id="s1", nama_produk="Ultra Milk 1L", harga=20000, ukuran=1000, satuan="ml", kategori="Susu & Olahan"),
            RecommendationCandidate(product_id="s2", nama_produk="Indomilk 1L", harga=18000, ukuran=1000, satuan="ml", kategori="Susu & Olahan"),
            RecommendationCandidate(product_id="m1", nama_produk="Aqua 600ml", harga=5000, ukuran=600, satuan="ml", kategori="Minuman"),
            RecommendationCandidate(product_id="m2", nama_produk="Teh Botol 350ml", harga=5000, ukuran=350, satuan="ml", kategori="Minuman"),
            RecommendationCandidate(product_id="r1", nama_produk="Rinso 800ml", harga=15000, ukuran=800, satuan="ml", kategori="Produk Rumah Tangga"),
        ]

        res = evaluate_best_value_logic(items)

        self.assertEqual(res.total_evaluated, 5)
        self.assertEqual(res.total_valid, 5)
        self.assertEqual(len(res.categories), 3)

        categories_dict = {cat.kategori: cat for cat in res.categories}
        self.assertIn("Susu & Olahan", categories_dict)
        self.assertIn("Minuman", categories_dict)
        self.assertIn("Produk Rumah Tangga", categories_dict)

        # Check Susu & Olahan winner = Indomilk
        susu_group = categories_dict["Susu & Olahan"].dimension_groups[0]
        self.assertTrue(susu_group.is_comparable)
        self.assertEqual(susu_group.best_value.product_id, "s2")

        # Check Minuman winner = Aqua
        minuman_group = categories_dict["Minuman"].dimension_groups[0]
        self.assertTrue(minuman_group.is_comparable)
        self.assertEqual(minuman_group.best_value.product_id, "m1")

        # Test Case 4: Single product in category -> is_comparable = False, no misleading Best Value claim
        rinso_group = categories_dict["Produk Rumah Tangga"].dimension_groups[0]
        self.assertFalse(rinso_group.is_comparable)
        self.assertIsNone(rinso_group.best_value)
        self.assertEqual(rinso_group.ranked_items[0].explanation, "Belum ada produk pembanding yang compatible dalam kelompok dimensi ini.")

    def test_same_category_different_dimensions(self):
        # Test Case 3: Same category but different dimensions (ml vs g) -> produce two separate dimension groups
        items = [
            RecommendationCandidate(product_id="s1", nama_produk="Susu A 1000ml", harga=20000, ukuran=1000, satuan="ml", kategori="Susu & Olahan"),
            RecommendationCandidate(product_id="s2", nama_produk="Susu B 500ml", harga=8000, ukuran=500, satuan="ml", kategori="Susu & Olahan"),
            RecommendationCandidate(product_id="y1", nama_produk="Yogurt A 100g", harga=5000, ukuran=100, satuan="g", kategori="Susu & Olahan"),
            RecommendationCandidate(product_id="y2", nama_produk="Yogurt B 200g", harga=8000, ukuran=200, satuan="g", kategori="Susu & Olahan"),
        ]

        res = evaluate_best_value_logic(items)

        self.assertEqual(res.total_evaluated, 4)
        self.assertEqual(res.total_valid, 4)
        self.assertEqual(len(res.categories), 1)

        cat_susu = res.categories[0]
        self.assertEqual(len(cat_susu.dimension_groups), 2)

        dim_groups_dict = {dg.dimension: dg for dg in cat_susu.dimension_groups}
        self.assertIn("volume", dim_groups_dict)
        self.assertIn("weight", dim_groups_dict)

        # Volume group winner = Susu B (8000/500 = 16 vs 20000/1000 = 20)
        vol_group = dim_groups_dict["volume"]
        self.assertTrue(vol_group.is_comparable)
        self.assertEqual(vol_group.best_value.product_id, "s2")

        # Weight group winner = Yogurt B (8000/200 = 40 vs 5000/100 = 50)
        weight_group = dim_groups_dict["weight"]
        self.assertTrue(weight_group.is_comparable)
        self.assertEqual(weight_group.best_value.product_id, "y2")

    def test_lainnya_and_legacy_general_category(self):
        # Test Case 6, 7 & 8: "Lainnya", legacy "General", and invalid/null category
        items = [
            RecommendationCandidate(product_id="p1", nama_produk="Produk A", harga=10000, ukuran=100, satuan="g", kategori="Lainnya"),
            RecommendationCandidate(product_id="p2", nama_produk="Produk B", harga=15000, ukuran=200, satuan="g", kategori="General"),
            RecommendationCandidate(product_id="p3", nama_produk="Produk C", harga=12000, ukuran=150, satuan="g", kategori=None),
            RecommendationCandidate(product_id="p4", nama_produk="Produk D", harga=20000, ukuran=250, satuan="g", kategori="KategoriAneh"),
        ]

        res = evaluate_best_value_logic(items)

        self.assertEqual(res.total_evaluated, 4)
        self.assertEqual(res.total_valid, 4)
        self.assertEqual(len(res.categories), 1)
        # All invalid/legacy categories should fall back to "Lainnya"
        self.assertEqual(res.categories[0].kategori, "Lainnya")

    def test_invalid_items_exclusion(self):
        items = [
            RecommendationCandidate(product_id="valid", nama_produk="Susu Valid", harga=15000, ukuran=250, satuan="ml", kategori="Susu & Olahan"),
            RecommendationCandidate(product_id="no_price", nama_produk="Susu Free", harga=0, ukuran=250, satuan="ml", kategori="Susu & Olahan"),
            RecommendationCandidate(product_id="no_size", nama_produk="Susu Micro", harga=10000, ukuran=0, satuan="ml", kategori="Susu & Olahan"),
            RecommendationCandidate(product_id="bad_unit", nama_produk="Susu Kotak", harga=10000, ukuran=1, satuan="box", kategori="Susu & Olahan"),
            RecommendationCandidate(product_id="no_name", nama_produk="", harga=10000, ukuran=200, satuan="ml", kategori="Susu & Olahan"),
        ]

        res = evaluate_best_value_logic(items)

        self.assertEqual(res.total_evaluated, 5)
        self.assertEqual(res.total_valid, 1)
        self.assertEqual(res.total_excluded, 4)
        self.assertEqual(len(res.excluded_items), 4)

    def test_recommendation_api_endpoint(self):
        # Test Case 9: API endpoint POST /recommendation/evaluate
        payload = {
            "items": [
                {"product_id": "p1", "nama_produk": "Susu A", "harga": 20000, "ukuran": 200, "satuan": "ml", "kategori": "Susu & Olahan"},
                {"product_id": "p2", "nama_produk": "Susu B", "harga": 25000, "ukuran": 350, "satuan": "ml", "kategori": "Susu & Olahan"},
                {"product_id": "p3", "nama_produk": "Susu C", "harga": 30000, "ukuran": 500, "satuan": "ml", "kategori": "Susu & Olahan"},
                {"product_id": "p4", "nama_produk": "Susu Invalid", "harga": -5000, "ukuran": 200, "satuan": "ml", "kategori": "Susu & Olahan"}
            ]
        }

        response = self.client.post("/recommendation/evaluate", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertEqual(data["total_evaluated"], 4)
        self.assertEqual(data["total_valid"], 3)
        self.assertEqual(data["total_excluded"], 1)
        self.assertEqual(len(data["categories"]), 1)

        dim_group = data["categories"][0]["dimension_groups"][0]
        self.assertTrue(dim_group["is_comparable"])
        self.assertEqual(dim_group["best_value"]["product_id"], "p3")
        self.assertTrue(dim_group["best_value"]["is_best_value"])
        self.assertEqual(dim_group["best_value"]["badge"], "BEST VALUE")

    def test_get_recommended_products_endpoint(self):
        # Test GET /recommendation/recommended-products
        response = self.client.get("/recommendation/recommended-products")
        self.assertEqual(response.status_code, 200)
        items = response.json()
        self.assertIsInstance(items, list)
        self.assertGreater(len(items), 0)
        first_item = items[0]
        self.assertIn("id", first_item)
        self.assertIn("nama", first_item)
        self.assertIn("harga", first_item)
        self.assertIn("nama_toko", first_item)
        self.assertIn("updated_at", first_item)

if __name__ == "__main__":
    unittest.main()
