import os
import unittest
from fastapi.testclient import TestClient

from app.main import app, STATIC_DIR


class TestStaticHeaders(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.test_file_path = os.path.join(STATIC_DIR, "test_file.txt")
        with open(self.test_file_path, "w", encoding="utf-8") as f:
            f.write("test static content")

    def tearDown(self):
        if os.path.exists(self.test_file_path):
            os.remove(self.test_file_path)

    def test_static_file_response_contains_security_and_cache_headers(self):
        res = self.client.get("/static/test_file.txt")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("x-content-type-options"), "nosniff")
        cache_control = res.headers.get("cache-control", "")
        self.assertIn("public", cache_control)
        self.assertIn("max-age=", cache_control)

    def test_static_head_request_contains_headers(self):
        res = self.client.head("/static/test_file.txt")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("x-content-type-options"), "nosniff")
        cache_control = res.headers.get("cache-control", "")
        self.assertIn("public", cache_control)
        self.assertIn("max-age=", cache_control)

    def test_static_subfolder_file_contains_headers(self):
        res = self.client.get("/static/uploads/products/.gitkeep")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("x-content-type-options"), "nosniff")
        cache_control = res.headers.get("cache-control", "")
        self.assertIn("public", cache_control)
        self.assertIn("max-age=", cache_control)

    def test_non_static_route_does_not_force_static_headers(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        # Root endpoint shouldn't get the static cache-control unless intentionally configured
        self.assertNotEqual(res.headers.get("cache-control"), "public, max-age=86400")


if __name__ == "__main__":
    unittest.main()
