import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.dependencies import get_current_user, get_current_admin_user, get_current_user_profile
from app.models.db_models import Product, User

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

        # Regular user
        reg_user = User(
            id=TEST_USER_ID,
            nama="Budi Santoso",
            email="budi@example.com",
            role="user"
        )
        # Admin user
        admin_user = User(
            id=TEST_ADMIN_ID,
            nama="Admin Rakoon",
            email="admin@rakoon.app",
            role="admin"
        )
        self.db.add(reg_user)
        self.db.add(admin_user)
        self.db.commit()

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_get_my_profile_regular_user(self):
        app.dependency_overrides[get_current_user] = lambda: TEST_USER_ID
        res = self.client.get("/auth/me")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["user_id"], TEST_USER_ID)
        self.assertEqual(data["role"], "user")

    def test_get_my_profile_admin_user(self):
        app.dependency_overrides[get_current_user] = lambda: TEST_ADMIN_ID
        res = self.client.get("/auth/me")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["user_id"], TEST_ADMIN_ID)
        self.assertEqual(data["role"], "admin")

    def test_get_my_profile_admin_via_env(self):
        env_user_id = "33333333-3333-3333-3333-333333333333"
        user_with_env_email = User(
            id=env_user_id,
            nama="Super Admin",
            email="superadmin@rakoon.com",
            role="user"
        )
        self.db.add(user_with_env_email)
        self.db.commit()

        os.environ["ADMIN_EMAILS"] = "superadmin@rakoon.com,boss@rakoon.com"
        try:
            app.dependency_overrides[get_current_user] = lambda: env_user_id
            res = self.client.get("/auth/me")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["role"], "admin")
        finally:
            os.environ.pop("ADMIN_EMAILS", None)

    def test_update_photo_url_forbidden_for_regular_user(self):
        app.dependency_overrides[get_current_user] = lambda: TEST_USER_ID
        res = self.client.put(
            f"/products/{self.prod_id}/photo",
            json={"foto_url": "https://images.example.com/susu.png"}
        )
        self.assertEqual(res.status_code, 403)
        self.assertIn("Hanya admin", res.json()["detail"])

    def test_update_photo_url_success_for_admin(self):
        app.dependency_overrides[get_current_user] = lambda: TEST_ADMIN_ID
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
        app.dependency_overrides[get_current_user] = lambda: TEST_ADMIN_ID
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
        app.dependency_overrides[get_current_user] = lambda: TEST_ADMIN_ID
        fake_file = io.BytesIO(b"\x89PNG\r\n\x1a\nFake PNG file binary content")
        res = self.client.post(
            f"/products/{self.prod_id}/upload-photo",
            files={"file": ("susu.png", fake_file, "image/png")}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["foto_url"].startswith("/static/uploads/products/"))
        self.assertTrue(data["foto_url"].endswith(".png"))

        # Verify static file serves
        static_res = self.client.get(data["foto_url"])
        self.assertEqual(static_res.status_code, 200)
        self.assertEqual(static_res.content, b"\x89PNG\r\n\x1a\nFake PNG file binary content")

    def test_upload_photo_invalid_type_rejected(self):
        app.dependency_overrides[get_current_user] = lambda: TEST_ADMIN_ID
        fake_text = io.BytesIO(b"Hello world text file")
        res = self.client.post(
            f"/products/{self.prod_id}/upload-photo",
            files={"file": ("notes.txt", fake_text, "text/plain")}
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Format file tidak valid", res.json()["detail"])

    def test_catalog_shows_uploaded_photo(self):
        app.dependency_overrides[get_current_user] = lambda: TEST_ADMIN_ID
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
