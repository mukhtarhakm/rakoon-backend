import unittest
from fastapi.testclient import TestClient
from app.main import app

class TestProductsApi(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_get_products_list(self):
        response = self.client.get("/products/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)

    def test_get_products_with_search_and_category(self):
        response = self.client.get("/products/?search=indomie&category=Makanan Instan")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)

    def test_get_products_catalog(self):
        response = self.client.get("/products/catalog?limit=5")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)

if __name__ == "__main__":
    unittest.main()
