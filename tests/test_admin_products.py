import io
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.dependencies import get_current_user, get_current_admin_user, get_current_user_profile
from app.models.db_models import Product

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


TEST_USER_ID = "11111111-1111-1111-1111-111111111111"
TEST_ADMIN_ID = "99999999-9999-9999-9999-999999999999"


class TestAdminProducts(unittest.TestCase):

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()

        app.dependency_overrides[get_db] = override_get_db

        self.client = TestClient(app)

        self.prod_id = "d1111111-1111-1111-1111-111111111111"
        prod = Product(
            id=self.prod_id,
            nama="Susu Kotak Cokelat 1L",
            kategori="Minuman",
            ukuran=1.0,
            satuan="l",
            foto_url=None
        )
        self.db.add(prod)

        self.db.commit()

        # Mock Supabase Storage to prevent polluting live storage with test artifacts
        self.mock_sb = MagicMock()
        mock_bucket = MagicMock()
        def fake_get_public_url(fname):
            return f"https://mock.supabase.co/storage/v1/object/public/product-photos/{fname}"
        mock_bucket.get_public_url.side_effect = fake_get_public_url
        mock_bucket.upload.return_value = {"Key": "mocked"}
        self.mock_sb.storage.from_.return_value = mock_bucket

        self.mock_sb_patch = patch("app.services.storage_upload.supabase", self.mock_sb)
        self.mock_sb_patch.start()

    def tearDown(self):
        self.mock_sb_patch.stop()
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _as_admin(self):
        # Opsi B: admin ditentukan email ADMIN_EMAILS + Supabase Admin API (diuji di app/tests/test_auth.py),
        # jadi test endpoint admin cukup melewati gerbangnya.
        app.dependency_overrides[get_current_user] = lambda: TEST_ADMIN_ID
        app.dependency_overrides[get_current_admin_user] = lambda: {"user_id": TEST_ADMIN_ID, "role": "admin"}

    def _get_profile(self, user_id, claims, admin_emails="", admin_api=None, admin_api_status=200):
        """GET /auth/me dengan klaim token terverifikasi di-mock dan Supabase Admin API di-mock."""
        app.dependency_overrides[get_current_user] = lambda: user_id
        api_response = MagicMock()
        api_response.status_code = admin_api_status
        api_response.json.return_value = admin_api if admin_api is not None else {}
        if admin_api_status != 200:
            api_response.raise_for_status.side_effect = Exception("HTTP Error")
        with patch("app.dependencies.verify_access_token", return_value={"sub": user_id, **claims}), \
                patch("app.dependencies.httpx.get", return_value=api_response) as mock_get, \
                patch.dict(os.environ, {"ADMIN_EMAILS": admin_emails, "SUPABASE_SERVICE_KEY": "test-service-key-not-real"}):
            import app.dependencies as deps
            getattr(deps, "_admin_confirm_cache", {}).clear()
            res = self.client.get("/auth/me", headers={"Authorization": "Bearer test-token"})
        return res, mock_get

    def test_get_my_profile_regular_user(self):
        res, mock_get = self._get_profile(TEST_USER_ID, {"email": "budi@example.com"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["user_id"], TEST_USER_ID)
        self.assertEqual(data["role"], "user")
        mock_get.assert_not_called()

    def test_profile_never_touches_the_database(self):
        # Nama/email profil hanya dari klaim token; tabel users sudah tidak dipakai.
        spy_db = MagicMock()
        app.dependency_overrides[get_db] = lambda: spy_db
        res, _ = self._get_profile(TEST_ADMIN_ID, {"email": "admin@example.com"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["role"], "user")
        spy_db.query.assert_not_called()

    def test_get_my_profile_admin_via_env_with_confirmed_email(self):
        res, mock_get = self._get_profile(
            TEST_USER_ID,
            {"email": "superadmin@example.com"},
            admin_emails="superadmin@example.com,boss@example.com",
            admin_api={"email": "superadmin@example.com", "email_confirmed_at": "2026-01-01T00:00:00Z"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["role"], "admin")
        mock_get.assert_called_once()

    def test_get_my_profile_env_admin_with_unconfirmed_email_is_user(self):
        res, _ = self._get_profile(
            TEST_USER_ID,
            {"email": "superadmin@example.com"},
            admin_emails="superadmin@example.com",
            admin_api={"email": "superadmin@example.com", "email_confirmed_at": None},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["role"], "user")

    def test_get_my_profile_env_admin_is_user_when_admin_api_fails(self):
        res, _ = self._get_profile(
            TEST_USER_ID,
            {"email": "superadmin@example.com"},
            admin_emails="superadmin@example.com",
            admin_api_status=500,
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["role"], "user")

    def test_update_photo_url_forbidden_for_regular_user(self):
        app.dependency_overrides[get_current_user] = lambda: TEST_USER_ID
        res = self.client.put(
            f"/products/{self.prod_id}/photo",
            json={"foto_url": "https://images.example.com/susu.png"}
        )
        self.assertEqual(res.status_code, 403)
        self.assertIn("Hanya admin", res.json()["detail"])

    def test_update_photo_url_success_for_admin(self):
        self._as_admin()
        test_url = "https://images.example.com/susu-cokelat.png"
        res = self.client.put(
            f"/products/{self.prod_id}/photo",
            json={"foto_url": test_url}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["id"], self.prod_id)
        self.assertEqual(data["foto_url"], test_url)

        # Verify in DB
        prod = self.db.query(Product).filter(Product.id == self.prod_id).first()
        self.assertEqual(prod.foto_url, test_url)

    def test_update_photo_url_not_found(self):
        self._as_admin()
        res = self.client.put(
            "/products/nonexistent-id/photo",
            json={"foto_url": "https://example.com/image.png"}
        )
        self.assertEqual(res.status_code, 404)

    def test_upload_photo_forbidden_for_regular_user(self):
        app.dependency_overrides[get_current_user] = lambda: TEST_USER_ID
        fake_file = io.BytesIO(b"\x89PNG\r\n\x1a\nFake PNG content")
        res = self.client.post(
            f"/products/{self.prod_id}/upload-photo",
            files={"file": ("product.png", fake_file, "image/png")}
        )
        self.assertEqual(res.status_code, 403)

    def test_upload_photo_success_for_admin(self):
        self._as_admin()
        fake_file = io.BytesIO(b"\x89PNG\r\n\x1a\nFake PNG file binary content")
        res = self.client.post(
            f"/products/{self.prod_id}/upload-photo",
            files={"file": ("susu.png", fake_file, "image/png")}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        # URL now comes from Supabase Storage (https://...)
        self.assertTrue(data["foto_url"].startswith("https://"))
        self.assertTrue(data["foto_url"].rstrip("?").endswith(".png"))

    def test_upload_photo_jpg_with_octet_stream_success(self):
        """Admin can upload .jpg file even when client sends application/octet-stream or generic content-type"""
        self._as_admin()
        fake_jpg = io.BytesIO(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01Fake JPEG content")
        res = self.client.post(
            f"/products/{self.prod_id}/upload-photo",
            files={"file": ("product.jpg", fake_jpg, "application/octet-stream")}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["foto_url"].startswith("https://"))
        self.assertTrue(data["foto_url"].rstrip("?").endswith(".jpg"))

    def test_upload_photo_invalid_type_rejected(self):
        self._as_admin()
        fake_text = io.BytesIO(b"Hello world text file")
        res = self.client.post(
            f"/products/{self.prod_id}/upload-photo",
            files={"file": ("notes.txt", fake_text, "text/plain")}
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Format file tidak valid", res.json()["detail"])

    def _stored_foto_url(self):
        self.db.expire_all()
        return self.db.query(Product).filter(Product.id == self.prod_id).first().foto_url

    def test_upload_photo_storage_failure_returns_503_and_keeps_old_photo(self):
        self._as_admin()
        self.mock_sb.storage.from_.return_value.upload.side_effect = Exception("storage down")
        res = self.client.post(
            f"/products/{self.prod_id}/upload-photo",
            files={"file": ("susu.png", io.BytesIO(b"\x89PNG\r\n\x1a\nbody"), "image/png")}
        )
        self.assertEqual(res.status_code, 503)
        self.assertIsNone(self._stored_foto_url())

    def test_upload_photo_with_mock_client_returns_503_not_local_path(self):
        from app.database import MockSupabaseClient
        self._as_admin()
        with patch("app.services.storage_upload.supabase", MockSupabaseClient()):
            res = self.client.post(
                f"/products/{self.prod_id}/upload-photo",
                files={"file": ("susu.png", io.BytesIO(b"\x89PNG\r\n\x1a\nbody"), "image/png")}
            )
        self.assertEqual(res.status_code, 503)
        self.assertIsNone(self._stored_foto_url())

    def test_upload_photo_fake_png_by_extension_and_content_type_rejected(self):
        self._as_admin()
        res = self.client.post(
            f"/products/{self.prod_id}/upload-photo",
            files={"file": ("evil.png", io.BytesIO(b"<html>not an image</html>"), "image/png")}
        )
        self.assertEqual(res.status_code, 400)
        self.mock_sb.storage.from_.return_value.upload.assert_not_called()

    def test_upload_photo_over_5mb_returns_413(self):
        self._as_admin()
        big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024)
        res = self.client.post(
            f"/products/{self.prod_id}/upload-photo",
            files={"file": ("big.png", io.BytesIO(big), "image/png")}
        )
        self.assertEqual(res.status_code, 413)

    def test_catalog_shows_uploaded_photo(self):
        self._as_admin()
        test_url = "https://images.example.com/susu-cokelat.png"
        self.client.put(
            f"/products/{self.prod_id}/photo",
            json={"foto_url": test_url}
        )

        catalog_res = self.client.get("/products/catalog")
        self.assertEqual(catalog_res.status_code, 200)
        items = catalog_res.json()
        matched = [i for i in items if i["id"] == self.prod_id]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["foto_url"], test_url)

    def test_get_product_by_id_returns_photo_url(self):
        """GET /products/{id} returns product details including updated foto_url"""
        test_url = "https://images.example.com/susu-detail.jpg"
        prod = self.db.query(Product).filter(Product.id == self.prod_id).first()
        prod.foto_url = test_url
        self.db.commit()

        res = self.client.get(f"/products/{self.prod_id}")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["id"], self.prod_id)
        self.assertEqual(data["foto_url"], test_url)

    def test_price_compare_contains_product_photo_url(self):
        """GET /price/compare/{id} response includes foto_url for product detail synchronization"""
        test_url = "https://images.example.com/susu-compare.jpg"
        prod = self.db.query(Product).filter(Product.id == self.prod_id).first()
        prod.foto_url = test_url
        self.db.commit()

        res = self.client.get(f"/price/compare/{self.prod_id}?lat=-7.56&lng=110.82")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["foto_url"], test_url)

    def test_upload_photo_db_work_runs_off_event_loop_thread(self):
        import threading
        from sqlalchemy.orm import Session
        from app.routers import products

        self._as_admin()
        loop_threads, db_threads = set(), set()
        real_upload, real_query, real_commit = products.upload_image, Session.query, Session.commit

        async def upload(*args, **kwargs):
            loop_threads.add(threading.get_ident())
            return await real_upload(*args, **kwargs)

        def query(session, *args, **kwargs):
            db_threads.add(threading.get_ident())
            return real_query(session, *args, **kwargs)

        def commit(session):
            db_threads.add(threading.get_ident())
            return real_commit(session)

        png = io.BytesIO(b"\x89PNG\r\n\x1a\nbody")
        with patch.object(products, "upload_image", upload), \
                patch.object(Session, "query", query), patch.object(Session, "commit", commit):
            res = self.client.post(
                f"/products/{self.prod_id}/upload-photo",
                files={"file": ("susu.png", png, "image/png")}
            )

        self.assertEqual(res.status_code, 200)
        self.assertTrue(loop_threads and db_threads)
        self.assertTrue(db_threads.isdisjoint(loop_threads))
