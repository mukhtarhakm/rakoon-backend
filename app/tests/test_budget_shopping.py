import unittest
from fastapi.testclient import TestClient
from app.main import app

class TestBudgetShoppingAssistant(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(app)

    def test_health_check(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)

    def test_budget_shopping_recommend_endpoint_empty_items(self):
        # Empty items payload should trigger 422 Unprocessable Entity due to Pydantic min_length validation
        payload = {
            "budget": 100000,
            "items": []
        }
        response = self.client.post("/budget-shopping/recommend", json=payload)
        self.assertEqual(response.status_code, 422)

    def test_budget_shopping_recommend_endpoint_valid_payload(self):
        # Test endpoint with sample product IDs
        payload = {
            "budget": 100000,
            "items": [
                {"product_id": "dummy-p1", "qty": 2},
                {"product_id": "dummy-p2", "qty": 1}
            ]
        }
        response = self.client.post("/budget-shopping/recommend", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertEqual(data["budget"], 100000.0)
        # Should gracefully return no full match if dummy products are not in DB
        self.assertIn("is_full_match", data)
        self.assertIn("explanation", data)

if __name__ == "__main__":
    unittest.main()
