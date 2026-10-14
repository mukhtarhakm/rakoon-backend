import unittest
import logging
from unittest.mock import patch, MagicMock
from fastapi import HTTPException, status
from fastapi.testclient import TestClient

from app.main import app
from app.services.errors import safe_error
from app.database import get_db
from app.dependencies import get_current_user, get_current_admin_user


class TestSafeError(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.dummy_logger = logging.getLogger("test_safe_error_logger")

    def test_safe_error_unit_hides_raw_exception_detail(self):
        raw_error = Exception("psycopg2.errors.SyntaxError: syntax error at or near 'SELECT secret_column FROM users'")
        with patch.object(self.dummy_logger, "error") as mock_log_err:
            http_exc = safe_error(
                self.dummy_logger,
                raw_error,
                public_message="Terjadi kesalahan saat memproses data.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                log_prefix="Test operation",
            )
            self.assertEqual(http_exc.status_code, 500)
            self.assertEqual(http_exc.detail, "Terjadi kesalahan saat memproses data.")
            self.assertNotIn("secret_column", http_exc.detail)
            self.assertNotIn("psycopg2", http_exc.detail)
            mock_log_err.assert_called_once()
            args, kwargs = mock_log_err.call_args
            self.assertIn("Test operation: psycopg2.errors.SyntaxError", args[0])
            self.assertTrue(kwargs.get("exc_info"))

    def test_safe_error_preserves_existing_http_exception(self):
        orig_exc = HTTPException(status_code=404, detail="Item tidak ditemukan.")
        res_exc = safe_error(self.dummy_logger, orig_exc, public_message="Fallback")
        self.assertIs(res_exc, orig_exc)

    def test_price_endpoints_do_not_leak_raw_exception(self):
        app.dependency_overrides[get_current_user] = lambda: "user-123"
        mock_db = MagicMock()
        mock_db.add.side_effect = Exception("FATAL: connection leaked secret_token_xyz")
        mock_db.query.side_effect = Exception("FATAL: query leaked table_schema_abc")
        app.dependency_overrides[get_db] = lambda: mock_db

        try:
            # POST /price/
            res_post = self.client.post("/price/", json={
                "product_id": "00000000-0000-0000-0000-000000000001",
                "store_id": "00000000-0000-0000-0000-000000000002",
                "harga": 15000,
                "sumber_user_id": "user-123"
            })
            self.assertEqual(res_post.status_code, 500)
            self.assertNotIn("secret_token_xyz", res_post.text)
            self.assertIn("Terjadi kesalahan saat menyimpan data harga.", res_post.json().get("detail", ""))

            # GET /price/product/{product_id}
            res_hist = self.client.get("/price/product/00000000-0000-0000-0000-000000000001")
            self.assertEqual(res_hist.status_code, 500)
            self.assertNotIn("table_schema_abc", res_hist.text)
            self.assertIn("Terjadi kesalahan saat mengambil riwayat harga.", res_hist.json().get("detail", ""))

            # GET /price/compare/{product_id}
            res_comp = self.client.get("/price/compare/00000000-0000-0000-0000-000000000001?lat=-7.7&lng=110.4")
            self.assertEqual(res_comp.status_code, 500)
            self.assertNotIn("table_schema_abc", res_comp.text)
            self.assertIn("Terjadi kesalahan saat memproses perbandingan harga.", res_comp.json().get("detail", ""))
        finally:
            app.dependency_overrides.pop(get_current_user, None)
            app.dependency_overrides.pop(get_db, None)

    def test_stores_post_does_not_leak_raw_exception(self):
        app.dependency_overrides[get_current_user] = lambda: "user-123"
        mock_db = MagicMock()
        mock_db.commit.side_effect = Exception("PG_EXCEPTION: relation stores_hidden_data violates check constraint")
        app.dependency_overrides[get_db] = lambda: mock_db

        try:
            res = self.client.post("/stores/", json={
                "nama": "Toko Rahasia",
                "lat": -7.78,
                "lng": 110.40,
                "alamat": "Jl. Kaliurang"
            })
            self.assertEqual(res.status_code, 500)
            self.assertNotIn("stores_hidden_data", res.text)
            self.assertNotIn("PG_EXCEPTION", res.text)
            self.assertEqual(res.json().get("detail"), "Gagal menyimpan data toko ke database.")
        finally:
            app.dependency_overrides.pop(get_current_user, None)
            app.dependency_overrides.pop(get_db, None)

    def test_budget_shopping_does_not_leak_raw_exception(self):
        mock_db = MagicMock()
        mock_db.query.side_effect = Exception("SQLAlchemyError: raw table structure leaked: users_audit_table")
        app.dependency_overrides[get_db] = lambda: mock_db

        try:
            res = self.client.post("/budget-shopping/recommend", json={
                "budget": 50000,
                "items": [{"product_id": "00000000-0000-0000-0000-000000000001", "qty": 1}]
            })
            self.assertEqual(res.status_code, 500)
            self.assertNotIn("users_audit_table", res.text)
            self.assertNotIn("SQLAlchemyError", res.text)
            self.assertEqual(res.json().get("detail"), "Terjadi kesalahan saat memproses rekomendasi budget shopping.")
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_recommendation_evaluate_does_not_leak_raw_exception(self):
        with patch("app.routers.recommendation.evaluate_best_value_logic", side_effect=Exception("Critical math error: variable _secret_state")):
            res = self.client.post("/recommendation/evaluate", json={
                "items": [{"nama_produk": "Susu", "harga": 10000, "ukuran": 1000, "satuan": "ml"}]
            })
            self.assertEqual(res.status_code, 500)
            self.assertNotIn("_secret_state", res.text)
            self.assertEqual(res.json().get("detail"), "Terjadi kesalahan saat memproses rekomendasi.")

    def test_scan_confirm_does_not_leak_raw_exception(self):
        app.dependency_overrides[get_current_user] = lambda: {"id": "user-123"}
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.scalar.return_value = 0  # kuota harian: belum terpakai
        mock_db.commit.side_effect = Exception("DB_LOCK_TIMEOUT: deadlocked on pg_catalog._secret_lock")
        app.dependency_overrides[get_db] = lambda: mock_db

        try:
            res = self.client.post("/scan/confirm", json={
                "store_id": "00000000-0000-0000-0000-000000000001",
                "user_id": "user-123",
                "items": [{"nama_produk": "Permen", "harga": 500, "kategori": "Makanan Ringan"}]
            })
            self.assertEqual(res.status_code, 500)
            self.assertNotIn("_secret_lock", res.text)
            self.assertNotIn("DB_LOCK_TIMEOUT", res.text)
            self.assertEqual(res.json().get("detail"), "Terjadi kesalahan saat menyimpan konfirmasi scan.")
        finally:
            app.dependency_overrides.pop(get_current_user, None)
            app.dependency_overrides.pop(get_db, None)

    def test_products_update_photo_does_not_leak_raw_exception(self):
        app.dependency_overrides[get_current_admin_user] = lambda: {"user_id": "admin-1"}
        mock_db = MagicMock()
        mock_product = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_product
        mock_db.commit.side_effect = Exception("IntegrityError: key (foto_url) leaked _secret_hash")
        app.dependency_overrides[get_db] = lambda: mock_db

        try:
            res = self.client.put("/products/00000000-0000-0000-0000-000000000001/photo", json={
                "foto_url": "https://example.com/photo.jpg"
            })
            self.assertEqual(res.status_code, 500)
            self.assertNotIn("_secret_hash", res.text)
            self.assertNotIn("IntegrityError", res.text)
            self.assertEqual(res.json().get("detail"), "Gagal memperbarui foto produk.")
        finally:
            app.dependency_overrides.pop(get_current_admin_user, None)
            app.dependency_overrides.pop(get_db, None)


if __name__ == "__main__":
    unittest.main()
