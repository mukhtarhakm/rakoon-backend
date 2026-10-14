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
from app.dependencies import get_current_admin_user, get_current_user
from app.models.db_models import PriceEntry, Product, Store

engine = create_engine(
    "sqlite:///:memory:",
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


USER_ID = "11111111-1111-1111-1111-111111111111"
ADMIN_EMAIL = "boss@example.com"


class TestAdminOnlyWrites(unittest.TestCase):
    """POST /price/ dan POST /stores/ hanya admin (P1-13/P5): pengguna biasa harus lewat /scan/confirm."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: USER_ID
        self.client = TestClient(app)

        self.store = Store(nama="Toko Uji", lat=-7.75, lng=110.4)
        self.product = Product(nama="Susu Uji 1L", kategori="Minuman", ukuran=1.0, satuan="l")
        self.db.add_all([self.store, self.product])
        self.db.commit()
        self.price_payload = {
            "product_id": str(self.product.id),
            "store_id": str(self.store.id),
            "harga": 15000,
            "sumber_user_id": "diabaikan",
        }
        self.store_payload = {"nama": "Toko Baru", "lat": -6.2, "lng": 106.8, "alamat": "Jl. Contoh No. 1"}

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _as_admin_gate(self):
        app.dependency_overrides[get_current_admin_user] = lambda: {"user_id": USER_ID, "role": "admin"}

    def _counts(self):
        self.db.expire_all()
        return self.db.query(Store).count(), self.db.query(PriceEntry).count()

    # --- pengguna biasa: 403, tidak ada yang tersimpan ---

    def test_regular_user_cannot_create_store(self):
        res = self.client.post("/stores/", json=self.store_payload)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self._counts(), (1, 0))

    def test_regular_user_cannot_create_price_entry(self):
        res = self.client.post("/price/", json=self.price_payload)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self._counts(), (1, 0))

    # --- tanpa login: 401 ---

    def test_anonymous_requests_get_401(self):
        app.dependency_overrides.pop(get_current_user, None)
        self.assertEqual(self.client.post("/stores/", json=self.store_payload).status_code, 401)
        self.assertEqual(self.client.post("/price/", json=self.price_payload).status_code, 401)
        self.assertEqual(self._counts(), (1, 0))

    # --- admin: lolos ---

    def test_admin_can_create_store(self):
        self._as_admin_gate()
        res = self.client.post("/stores/", json=self.store_payload)
        self.assertEqual(res.status_code, 201)
        self.assertIn("store_id", res.json())
        self.assertEqual(self._counts(), (2, 0))

    def test_admin_can_create_price_entry_and_identity_comes_from_the_token(self):
        self._as_admin_gate()
        res = self.client.post("/price/", json={**self.price_payload, "sumber_user_id": "dipalsukan"})
        self.assertEqual(res.status_code, 201)
        self.db.expire_all()
        entry = self.db.query(PriceEntry).first()
        self.assertEqual(entry.sumber_user_id, USER_ID)
        self.assertEqual(entry.harga, 15000)

    def test_real_admin_chain_email_in_admin_emails_with_confirmed_email_passes(self):
        api_response = MagicMock()
        api_response.json.return_value = {"email": ADMIN_EMAIL, "email_confirmed_at": "2026-01-01T00:00:00Z"}
        import app.dependencies as deps
        getattr(deps, "_admin_confirm_cache", {}).clear()
        with patch("app.dependencies.verify_access_token", return_value={"sub": USER_ID, "email": ADMIN_EMAIL}), \
                patch("app.dependencies.httpx.get", return_value=api_response), \
                patch.dict(os.environ, {"ADMIN_EMAILS": ADMIN_EMAIL, "SUPABASE_SERVICE_KEY": "test-service-key-not-real"}):
            headers = {"Authorization": "Bearer test-token"}
            self.assertEqual(self.client.post("/stores/", json=self.store_payload, headers=headers).status_code, 201)
            self.assertEqual(self.client.post("/price/", json=self.price_payload, headers=headers).status_code, 201)
        getattr(deps, "_admin_confirm_cache", {}).clear()


if __name__ == "__main__":
    unittest.main()
