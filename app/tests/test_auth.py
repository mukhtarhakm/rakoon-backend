import unittest
import os
import sys
import time
import json
import uuid
from unittest.mock import patch
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
import cryptography.hazmat.primitives.hashes as hashes
from jwt.algorithms import ECAlgorithm

# Ensure backend directory is in the path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi.testclient import TestClient
from app.main import app
from app.models.db_models import Store, Product, PriceEntry

class MockResponse:
    def __init__(self, json_data, status_code=200):
        self.json_data = json_data
        self.status_code = status_code

    def json(self):
        return self.json_data

    def raise_for_status(self):
        if self.status_code != 200:
            raise Exception("HTTP Error")

class TestAuth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Setup in-memory SQLite database engine
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.database import Base
        from app.models.db_models import Store, Product, PriceEntry
        
        from sqlalchemy.pool import StaticPool
        cls.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool
        )
        cls.TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)

        cls.client = TestClient(app)
        cls.test_kid = "test-key-id-1"
        cls.test_uuid = str(uuid.uuid4())
        
        # Import EXPECTED_ISSUER dynamically from dependencies
        from app.dependencies import EXPECTED_ISSUER
        cls.expected_issuer = EXPECTED_ISSUER
        
        # Generate Elliptic Curve keypair for tests
        cls.private_key = ec.generate_private_key(ec.SECP256R1())
        cls.public_key = cls.private_key.public_key()
        
        # Convert public key to JWK dictionary
        jwk_dict = json.loads(ECAlgorithm(hashes.SHA256).to_jwk(cls.public_key))
        jwk_dict["kid"] = cls.test_kid
        jwk_dict["alg"] = "ES256"
        jwk_dict["kty"] = "EC"
        jwk_dict["crv"] = "P-256"
        jwk_dict["use"] = "sig"
        
        cls.jwks_data = {
            "keys": [jwk_dict]
        }

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        # Override get_db locally for this test suite
        from app.database import get_db
        self.original_get_db = app.dependency_overrides.get(get_db)
        
        def override_get_db():
            db = self.TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()
                
        app.dependency_overrides[get_db] = override_get_db

        # Clean the tables before each test
        db = self.TestingSessionLocal()
        from app.models.db_models import Store, Product, PriceEntry
        db.query(PriceEntry).delete()
        db.query(Product).delete()
        db.query(Store).delete()
        db.commit()
        db.close()

    def tearDown(self):
        # Restore original get_db override
        from app.database import get_db
        if self.original_get_db is not None:
            app.dependency_overrides[get_db] = self.original_get_db
        else:
            app.dependency_overrides.pop(get_db, None)

    def _create_token(self, sub="DEFAULT", aud="authenticated", iss="DEFAULT", exp="DEFAULT", kid="DEFAULT", alg="ES256", key=None, iat="DEFAULT", extra=None):
        if sub == "DEFAULT":
            sub = self.test_uuid
        if iss == "DEFAULT":
            iss = self.expected_issuer
        if exp == "DEFAULT":
            exp = int(time.time()) + 3600  # 1 hour in the future
        if iat == "DEFAULT":
            iat = int(time.time())
        if kid == "DEFAULT":
            kid = self.test_kid
        if key is None:
            key = self.private_key
            
        headers = {}
        if kid is not None:
            headers["kid"] = kid
            
        payload = {}
        if sub is not None:
            payload["sub"] = sub
        if aud is not None:
            payload["aud"] = aud
        if iss is not None:
            payload["iss"] = iss
        if exp is not None:
            payload["exp"] = exp
        if iat is not None:
            payload["iat"] = iat
        if extra:
            payload.update(extra)

        # If signing with symmetric key for testing unsupported algorithms
        if alg == "HS256":
            return jwt.encode(payload, "secret", algorithm="HS256", headers=headers)
            
        return jwt.encode(payload, key, algorithm=alg, headers=headers)

    def _make_auth_request(self, token=None, headers=None):
        if headers is None:
            headers = {}
            if token is not None:
                headers["Authorization"] = f"Bearer {token}"
        return self.client.post(
            "/stores/",
            json={"nama": "Toko Uji", "lat": -6.2088, "lng": 106.8456},
            headers=headers
        )

    @patch("app.dependencies.httpx.get")
    def test_missing_authorization_header(self, mock_get):
        response = self._make_auth_request()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Not authenticated")

    @patch("app.dependencies.httpx.get")
    def test_invalid_bearer_format(self, mock_get):
        response = self._make_auth_request(headers={"Authorization": "Basic wrongformat"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Not authenticated")

    @patch("app.dependencies.httpx.get")
    def test_invalid_jwt_format(self, mock_get):
        response = self._make_auth_request(headers={"Authorization": "Bearer thisisnotajwttoken"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Not authenticated")

    @patch("app.dependencies.httpx.get")
    def test_missing_kid(self, mock_get):
        token = self._create_token(kid=None)
        response = self._make_auth_request(token)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Not authenticated")

    @patch("app.dependencies.httpx.get")
    def test_unsupported_algorithm(self, mock_get):
        token = self._create_token(alg="HS256")
        response = self._make_auth_request(token)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Not authenticated")

    @patch("app.dependencies.httpx.get")
    def test_unknown_kid(self, mock_get):
        mock_get.return_value = MockResponse(self.jwks_data, 200)
        token = self._create_token(kid="unknown-kid")
        response = self._make_auth_request(token)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Not authenticated")

    @patch("app.dependencies.httpx.get")
    def test_invalid_ec_key(self, mock_get):
        bad_jwk = {
            "kty": "RSA",
            "kid": "bad-kid-1",
            "alg": "ES256"
        }
        bad_jwks = {"keys": [bad_jwk]}
        mock_get.return_value = MockResponse(bad_jwks, 200)
        token = self._create_token(kid="bad-kid-1")
        response = self._make_auth_request(token)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Not authenticated")

    @patch("app.dependencies.httpx.get")
    def test_invalid_signature(self, mock_get):
        mock_get.return_value = MockResponse(self.jwks_data, 200)
        different_private_key = ec.generate_private_key(ec.SECP256R1())
        token = self._create_token(key=different_private_key)
        response = self._make_auth_request(token)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Not authenticated")

    @patch("app.dependencies.httpx.get")
    def test_expired_jwt(self, mock_get):
        mock_get.return_value = MockResponse(self.jwks_data, 200)
        token = self._create_token(exp=int(time.time()) - 90)
        response = self._make_auth_request(token)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Not authenticated")

    @patch("app.dependencies.httpx.get")
    def test_wrong_audience(self, mock_get):
        mock_get.return_value = MockResponse(self.jwks_data, 200)
        token = self._create_token(aud="incorrect-audience")
        response = self._make_auth_request(token)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Not authenticated")

    @patch("app.dependencies.httpx.get")
    def test_wrong_issuer(self, mock_get):
        mock_get.return_value = MockResponse(self.jwks_data, 200)
        token = self._create_token(iss="https://wrong.supabase.co/auth/v1")
        response = self._make_auth_request(token)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Not authenticated")

    @patch("app.dependencies.httpx.get")
    def test_missing_sub(self, mock_get):
        mock_get.return_value = MockResponse(self.jwks_data, 200)
        token = self._create_token(sub=None)
        response = self._make_auth_request(token)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Not authenticated")

    @patch("app.dependencies.httpx.get")
    def test_invalid_uuid_sub(self, mock_get):
        mock_get.return_value = MockResponse(self.jwks_data, 200)
        token = self._create_token(sub="not-a-valid-uuid-string")
        response = self._make_auth_request(token)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Not authenticated")

    @patch("app.dependencies.httpx.get")
    def test_valid_es256_jwt(self, mock_get):
        mock_get.return_value = MockResponse(self.jwks_data, 200)
        from app.dependencies import jwks_cache
        jwks_cache.keys = {}
        jwks_cache.last_fetched = 0.0
        token = self._create_token(sub=self.test_uuid)
        response = self._make_auth_request(token)
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertIn("store_id", data)

    @patch("app.dependencies.httpx.get")
    def test_confirm_scan_no_auth(self, mock_get):
        payload = {
            "store_id": "a5b07384-d113-4956-b51c-43f11075d654",
            "user_id": "attacker-user-id",
            "items": []
        }
        response = self.client.post("/scan/confirm", json=payload)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Not authenticated")
        
        # Database should be unchanged (no items)
        from app.models.db_models import PriceEntry
        db = self.TestingSessionLocal()
        self.assertEqual(db.query(PriceEntry).count(), 0)
        db.close()

    @patch("app.dependencies.httpx.get")
    def test_confirm_scan_invalid_jwt(self, mock_get):
        payload = {
            "store_id": "a5b07384-d113-4956-b51c-43f11075d654",
            "user_id": "attacker-user-id",
            "items": []
        }
        response = self.client.post(
            "/scan/confirm", 
            json=payload,
            headers={"Authorization": "Bearer bad-token"}
        )
        self.assertEqual(response.status_code, 401)
        
        # Database should be unchanged
        from app.models.db_models import PriceEntry
        db = self.TestingSessionLocal()
        self.assertEqual(db.query(PriceEntry).count(), 0)
        db.close()

    @patch("app.dependencies.httpx.get")
    def test_confirm_scan_success_valid_jwt(self, mock_get):
        mock_get.return_value = MockResponse(self.jwks_data, 200)
        
        # Seed store and product first in our test database
        from app.models.db_models import Store, Product, PriceEntry
        db = self.TestingSessionLocal()
        store = Store(id="a5b07384-d113-4956-b51c-43f11075d654", nama="Toko Uji", lat=-6.2088, lng=106.8456)
        db.add(store)
        db.commit()
        
        token = self._create_token(sub=self.test_uuid)
        
        payload = {
            "store_id": "a5b07384-d113-4956-b51c-43f11075d654",
            "user_id": "attacker-user-id",  # Attempt to override identity via body
            "items": [
                {
                    "nama_produk": "Susu UHT Strawberry 1L",
                    "harga": 17500,
                    "ukuran": 1000.0,
                    "satuan": "ml",
                    "kategori": "Minuman"
                }
            ]
        }
        
        response = self.client.post(
            "/scan/confirm",
            json=payload,
            headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["items_saved"], 1)
        self.assertEqual(data["products_created"], 1)
        
        # Verify saved price entry has the identity from JWT.sub, NOT request body
        price_entry = db.query(PriceEntry).first()
        self.assertIsNotNone(price_entry)
        self.assertEqual(price_entry.sumber_user_id, self.test_uuid)
        self.assertNotEqual(price_entry.sumber_user_id, "attacker-user-id")
        db.close()

    @patch("app.dependencies.httpx.get")
    def test_create_price_no_auth(self, mock_get):
        payload = {
            "product_id": "c5b07384-d113-4956-b51c-43f11075d654",
            "store_id": "a5b07384-d113-4956-b51c-43f11075d654",
            "harga": 15000,
            "sumber_user_id": "attacker-user-id"
        }
        response = self.client.post("/price/", json=payload)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Not authenticated")
        
        # DB remains empty
        from app.models.db_models import PriceEntry
        db = self.TestingSessionLocal()
        self.assertEqual(db.query(PriceEntry).count(), 0)
        db.close()

    @patch("app.dependencies.httpx.get")
    def test_create_price_invalid_jwt(self, mock_get):
        payload = {
            "product_id": "c5b07384-d113-4956-b51c-43f11075d654",
            "store_id": "a5b07384-d113-4956-b51c-43f11075d654",
            "harga": 15000,
            "sumber_user_id": "attacker-user-id"
        }
        response = self.client.post(
            "/price/", 
            json=payload,
            headers={"Authorization": "Bearer bad-token"}
        )
        self.assertEqual(response.status_code, 401)
        
        # DB remains empty
        from app.models.db_models import PriceEntry
        db = self.TestingSessionLocal()
        self.assertEqual(db.query(PriceEntry).count(), 0)
        db.close()

    @patch("app.dependencies.httpx.get")
    def test_create_price_valid_jwt(self, mock_get):
        mock_get.return_value = MockResponse(self.jwks_data, 200)
        
        # Seed store and product first
        from app.models.db_models import Store, Product, PriceEntry
        db = self.TestingSessionLocal()
        store = Store(id="a5b07384-d113-4956-b51c-43f11075d654", nama="Toko Uji")
        product = Product(id="c5b07384-d113-4956-b51c-43f11075d654", nama="Ultra Milk Cokelat 1L", kategori="Minuman", ukuran=1.0, satuan="L")
        db.add_all([store, product])
        db.commit()
        
        token = self._create_token(sub=self.test_uuid)
        payload = {
            "product_id": "c5b07384-d113-4956-b51c-43f11075d654",
            "store_id": "a5b07384-d113-4956-b51c-43f11075d654",
            "harga": 15000,
            "sumber_user_id": "any-id"
        }
        response = self.client.post(
            "/price/",
            json=payload,
            headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(response.status_code, 201)
        
        # Verify stored data
        price_entry = db.query(PriceEntry).first()
        self.assertIsNotNone(price_entry)
        self.assertEqual(price_entry.sumber_user_id, self.test_uuid)
        self.assertEqual(price_entry.status_verifikasi, "verified")
        db.close()

    @patch("app.dependencies.httpx.get")
    def test_create_price_body_user_id_cannot_spoof_identity(self, mock_get):
        mock_get.return_value = MockResponse(self.jwks_data, 200)
        
        from app.models.db_models import Store, Product, PriceEntry
        db = self.TestingSessionLocal()
        store = Store(id="a5b07384-d113-4956-b51c-43f11075d654", nama="Toko Uji")
        product = Product(id="c5b07384-d113-4956-b51c-43f11075d654", nama="Ultra Milk Cokelat 1L", kategori="Minuman", ukuran=1.0, satuan="L")
        db.add_all([store, product])
        db.commit()
        
        token = self._create_token(sub="b5b07384-d113-4956-b51c-43f11075d655")  # UUID_A
        payload = {
            "product_id": "c5b07384-d113-4956-b51c-43f11075d654",
            "store_id": "a5b07384-d113-4956-b51c-43f11075d654",
            "harga": 15000,
            "sumber_user_id": "attacker-user-id"  # UUID_B
        }
        response = self.client.post(
            "/price/",
            json=payload,
            headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(response.status_code, 201)
        
        # Verify stored user_id is UUID_A, NOT UUID_B
        price_entry = db.query(PriceEntry).first()
        self.assertIsNotNone(price_entry)
        self.assertEqual(price_entry.sumber_user_id, "b5b07384-d113-4956-b51c-43f11075d655")
        self.assertNotEqual(price_entry.sumber_user_id, "attacker-user-id")
        db.close()

    @patch("app.dependencies.httpx.get")
    def test_create_store_no_auth(self, mock_get):
        payload = {
            "nama": "Toko Sejahtera",
            "lat": -6.2088,
            "lng": 106.8456,
            "alamat": "Jl. Merdeka No. 10"
        }
        response = self.client.post("/stores/", json=payload)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Not authenticated")
        
        # Store count does not change
        from app.models.db_models import Store
        db = self.TestingSessionLocal()
        self.assertEqual(db.query(Store).count(), 0)
        db.close()

    @patch("app.dependencies.httpx.get")
    def test_create_store_invalid_jwt(self, mock_get):
        payload = {
            "nama": "Toko Sejahtera",
            "lat": -6.2088,
            "lng": 106.8456,
            "alamat": "Jl. Merdeka No. 10"
        }
        response = self.client.post(
            "/stores/",
            json=payload,
            headers={"Authorization": "Bearer bad-token"}
        )
        self.assertEqual(response.status_code, 401)
        
        # Store count does not change
        from app.models.db_models import Store
        db = self.TestingSessionLocal()
        self.assertEqual(db.query(Store).count(), 0)
        db.close()

    @patch("app.dependencies.httpx.get")
    def test_create_store_valid_jwt(self, mock_get):
        mock_get.return_value = MockResponse(self.jwks_data, 200)
        
        token = self._create_token(sub=self.test_uuid)
        payload = {
            "nama": "Toko Sejahtera",
            "lat": -6.2088,
            "lng": 106.8456,
            "alamat": "Jl. Merdeka No. 10"
        }
        response = self.client.post(
            "/stores/",
            json=payload,
            headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(response.status_code, 201)
        
        # Store is created in DB
        from app.models.db_models import Store
        db = self.TestingSessionLocal()
        self.assertEqual(db.query(Store).count(), 1)
        store = db.query(Store).first()
        self.assertEqual(store.nama, "Toko Sejahtera")
        db.close()

    @patch("app.dependencies.httpx.get")
    def test_jwt_iat_leeway_accepted(self, mock_get):
        mock_get.return_value = MockResponse(self.jwks_data, 200)
        
        # Token with iat 30 seconds in the future (within 60s leeway)
        future_time = int(time.time()) + 30
        token = self._create_token(iat=future_time, exp=future_time + 3600)
        
        response = self._make_auth_request(token)
        self.assertEqual(response.status_code, 201)
        self.assertIn("store_id", response.json())

    @patch("app.dependencies.httpx.get")
    def test_jwt_iat_beyond_leeway_rejected(self, mock_get):
        mock_get.return_value = MockResponse(self.jwks_data, 200)
        
        # Token with iat 90 seconds in the future (exceeds 60s leeway)
        far_future_time = int(time.time()) + 90
        token = self._create_token(iat=far_future_time, exp=far_future_time + 3600)
        
        response = self._make_auth_request(token)
        self.assertEqual(response.status_code, 401)

    # ------------------------------------------------------------------
    # Karakterisasi perilaku auth SAAT INI (P1-18).
    # Test bernama test_current_behavior_* mengunci perilaku yang diketahui
    # bermasalah. JANGAN diperbaiki di sini; selisihnya harus terlihat saat
    # opsi B dikerjakan (PERBAIKAN.md 2b, audit/P1_fix.md).
    # ------------------------------------------------------------------

    def _reset_jwks_cache(self):
        from app.dependencies import jwks_cache
        jwks_cache.keys = {}
        jwks_cache.last_fetched = 0.0

    def _clear_users(self):
        from app.models.db_models import User
        db = self.TestingSessionLocal()
        db.query(User).delete()
        db.commit()
        db.close()

    def _seed_user(self, role="user", email="db-user@example.com", nama="Nama DB"):
        from app.models.db_models import User
        self._clear_users()
        db = self.TestingSessionLocal()
        db.add(User(id=self.test_uuid, nama=nama, email=email, role=role))
        db.commit()
        db.close()
        self.addCleanup(self._clear_users)

    def _prepare_valid_jwks(self, mock_get):
        mock_get.return_value = MockResponse(self.jwks_data, 200)
        self._reset_jwks_cache()
        self.addCleanup(self._reset_jwks_cache)

    def _me(self, token, admin_emails="", client=None):
        # ADMIN_EMAILS selalu di-patch agar hasil tidak bergantung pada .env lokal.
        with patch.dict(os.environ, {"ADMIN_EMAILS": admin_emails}):
            return (client or self.client).get(
                "/auth/me", headers={"Authorization": f"Bearer {token}"}
            )

    def _put_photo(self, token, admin_emails=""):
        with patch.dict(os.environ, {"ADMIN_EMAILS": admin_emails}):
            return self.client.put(
                f"/products/{uuid.uuid4()}/photo",
                json={"foto_url": "https://example.com/a.png"},
                headers={"Authorization": f"Bearer {token}"},
            )

    # 1. Token valid tanpa klaim email

    @patch("app.dependencies.httpx.get")
    def test_auth_me_and_test_protected_require_token(self, mock_get):
        self.assertEqual(self.client.get("/auth/me").status_code, 401)
        self.assertEqual(self.client.get("/auth/test-protected").status_code, 401)

    @patch("app.dependencies.httpx.get")
    def test_current_behavior_valid_token_without_email_claim(self, mock_get):
        self._prepare_valid_jwks(mock_get)
        response = self._me(self._create_token(), admin_emails="admin@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"user_id": self.test_uuid, "email": None, "nama": None, "role": "user"},
        )

    @patch("app.dependencies.httpx.get")
    def test_current_behavior_token_without_email_uses_users_table_email_for_admin_emails(self, mock_get):
        # Email dari tabel users dipakai bila token tidak membawa email, lalu
        # dicocokkan dengan ADMIN_EMAILS. Diubah di gelombang b, P1-01/P1-02.
        self._prepare_valid_jwks(mock_get)
        self._seed_user(role="user", email="db-admin@example.com", nama="Admin DB")
        response = self._me(self._create_token(), admin_emails="db-admin@example.com")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["role"], "admin")
        self.assertEqual(data["email"], "db-admin@example.com")
        self.assertEqual(data["nama"], "Admin DB")

    # 2. Klaim role: app_metadata, user_metadata, role tingkat atas

    @patch("app.dependencies.httpx.get")
    def test_current_behavior_app_metadata_role_admin_grants_admin(self, mock_get):
        # Klaim token cukup untuk jadi admin. Diubah di gelombang b, P1-01.
        self._prepare_valid_jwks(mock_get)
        token = self._create_token(extra={"app_metadata": {"role": "admin"}})
        response = self._me(token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "admin")

    @patch("app.dependencies.httpx.get")
    def test_user_metadata_role_admin_is_not_honored_but_name_is_used(self, mock_get):
        self._prepare_valid_jwks(mock_get)
        token = self._create_token(
            extra={"user_metadata": {"role": "admin", "full_name": "Budi"}}
        )
        response = self._me(token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "user")
        self.assertEqual(response.json()["nama"], "Budi")

    @patch("app.dependencies.httpx.get")
    def test_current_behavior_top_level_role_admin_grants_admin(self, mock_get):
        # Klaim `role` tingkat atas ikut menentukan admin. Diubah di gelombang b, P1-06.
        self._prepare_valid_jwks(mock_get)
        response = self._me(self._create_token(extra={"role": "admin"}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "admin")

    @patch("app.dependencies.httpx.get")
    def test_top_level_role_authenticated_is_plain_user(self, mock_get):
        self._prepare_valid_jwks(mock_get)
        response = self._me(self._create_token(extra={"role": "authenticated"}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "user")

    @patch("app.dependencies.httpx.get")
    def test_current_behavior_null_user_metadata_returns_500(self, mock_get):
        # user_metadata bernilai null jatuh ke AttributeError -> 500, bukan 401/200.
        # Diubah di gelombang b, P1-07.
        self._prepare_valid_jwks(mock_get)
        client = TestClient(app, raise_server_exceptions=False)
        token = self._create_token(extra={"user_metadata": None})
        self.assertEqual(self._me(token, client=client).status_code, 500)

    @patch("app.dependencies.httpx.get")
    def test_current_behavior_null_app_metadata_returns_500(self, mock_get):
        # app_metadata null (bukan admin lewat jalur lain) -> AttributeError -> 500.
        # Diubah di gelombang b, P1-07.
        self._prepare_valid_jwks(mock_get)
        client = TestClient(app, raise_server_exceptions=False)
        token = self._create_token(extra={"app_metadata": None})
        self.assertEqual(self._me(token, client=client).status_code, 500)

    @patch("app.dependencies.httpx.get")
    def test_current_behavior_admin_endpoint_passes_with_app_metadata_claim(self, mock_get):
        # Gerbang admin lolos hanya dari klaim token (produk tidak ada -> 404, bukan 403).
        # Diubah di gelombang b, P1-01.
        self._prepare_valid_jwks(mock_get)
        token = self._create_token(extra={"app_metadata": {"role": "admin"}})
        self.assertEqual(self._put_photo(token).status_code, 404)

    @patch("app.dependencies.httpx.get")
    def test_admin_endpoint_rejects_plain_user_with_403(self, mock_get):
        self._prepare_valid_jwks(mock_get)
        self.assertEqual(self._put_photo(self._create_token()).status_code, 403)

    # 3. JWKS gagal diambil

    @patch("app.dependencies.httpx.get")
    def test_current_behavior_jwks_fetch_error_with_empty_cache_returns_401(self, mock_get):
        # Fail-closed, tetapi 401 (bukan 503): pengguna sah melihat "tidak terautentikasi"
        # saat Supabase gangguan. Diubah di gelombang b, P1-08.
        self._reset_jwks_cache()
        self.addCleanup(self._reset_jwks_cache)
        mock_get.side_effect = Exception("jwks down")
        response = self.client.get(
            "/auth/test-protected", headers={"Authorization": f"Bearer {self._create_token()}"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Not authenticated")
        self.assertEqual(mock_get.call_count, 1)

    @patch("app.dependencies.httpx.get")
    def test_current_behavior_jwks_http_error_with_empty_cache_returns_401(self, mock_get):
        self._reset_jwks_cache()
        self.addCleanup(self._reset_jwks_cache)
        mock_get.return_value = MockResponse({}, 503)
        response = self.client.get(
            "/auth/test-protected", headers={"Authorization": f"Bearer {self._create_token()}"}
        )
        self.assertEqual(response.status_code, 401)

    @patch("app.dependencies.httpx.get")
    def test_current_behavior_stale_jwks_keys_stay_valid_when_refresh_fails(self, mock_get):
        # Cache lewat TTL + fetch gagal: kunci lama tetap diterima tanpa batas umur.
        # Diubah di gelombang b, P1-08.
        from app.dependencies import jwks_cache
        self.addCleanup(self._reset_jwks_cache)
        jwks_cache.keys = {self.test_kid: self.jwks_data["keys"][0]}
        jwks_cache.last_fetched = time.time() - 3600
        mock_get.side_effect = Exception("jwks down")
        response = self.client.get(
            "/auth/test-protected", headers={"Authorization": f"Bearer {self._create_token()}"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"user_id": self.test_uuid})
        self.assertEqual(mock_get.call_count, 1)

    # 4. kid tidak dikenal

    @patch("app.dependencies.httpx.get")
    def test_current_behavior_unknown_kid_refetches_jwks_on_every_request(self, mock_get):
        # Tanpa cache negatif: tiap request dengan kid acak memaksa satu fetch sinkron.
        # Diubah di gelombang b, P1-08.
        self._prepare_valid_jwks(mock_get)
        ok = self.client.get(
            "/auth/test-protected", headers={"Authorization": f"Bearer {self._create_token()}"}
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(mock_get.call_count, 1)
        for i in range(3):
            token = self._create_token(kid=f"random-kid-{i}")
            response = self.client.get(
                "/auth/test-protected", headers={"Authorization": f"Bearer {token}"}
            )
            self.assertEqual(response.status_code, 401)
        self.assertEqual(mock_get.call_count, 4)

    # 5. Token kedaluwarsa dan alg selain ES256

    @patch("app.dependencies.httpx.get")
    def test_expired_token_within_leeway_is_accepted(self, mock_get):
        # exp 30 dtk lalu masih diterima (leeway 60 dtk); 90 dtk lalu ditolak
        # (test_expired_jwt).
        self._prepare_valid_jwks(mock_get)
        token = self._create_token(exp=int(time.time()) - 30)
        response = self.client.get(
            "/auth/test-protected", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(response.status_code, 200)

    @patch("app.dependencies.httpx.get")
    def test_alg_es384_rejected_before_jwks_fetch(self, mock_get):
        self._reset_jwks_cache()
        self.addCleanup(self._reset_jwks_cache)
        token = self._create_token(alg="ES384", key=ec.generate_private_key(ec.SECP384R1()))
        response = self.client.get(
            "/auth/test-protected", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(mock_get.call_count, 0)

    @patch("app.dependencies.httpx.get")
    def test_alg_none_rejected_before_jwks_fetch(self, mock_get):
        self._reset_jwks_cache()
        self.addCleanup(self._reset_jwks_cache)
        payload = {
            "sub": self.test_uuid,
            "aud": "authenticated",
            "iss": self.expected_issuer,
            "exp": int(time.time()) + 3600,
        }
        token = jwt.encode(payload, None, algorithm="none", headers={"kid": self.test_kid})
        response = self.client.get(
            "/auth/test-protected", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(mock_get.call_count, 0)

    # 6. Prioritas ADMIN_EMAILS vs users.role vs klaim JWT

    @patch("app.dependencies.httpx.get")
    def test_current_behavior_users_role_admin_grants_admin_without_env(self, mock_get):
        # users.role='admin' cukup, walau email token tidak ada di ADMIN_EMAILS.
        # Diubah di gelombang b, P1-01/P1-05.
        self._prepare_valid_jwks(mock_get)
        self._seed_user(role="admin")
        token = self._create_token(extra={"email": "other@example.com"})
        response = self._me(token, admin_emails="")
        self.assertEqual(response.json()["role"], "admin")

    @patch("app.dependencies.httpx.get")
    def test_current_behavior_admin_emails_overrides_users_role_user(self, mock_get):
        # ADMIN_EMAILS mengalahkan users.role='user'. Diubah di gelombang b, P1-01.
        self._prepare_valid_jwks(mock_get)
        self._seed_user(role="user", email="db-user@example.com")
        token = self._create_token(extra={"email": "boss@example.com"})
        response = self._me(token, admin_emails="boss@example.com")
        self.assertEqual(response.json()["role"], "admin")

    @patch("app.dependencies.httpx.get")
    def test_admin_emails_matches_token_email_not_users_table_email(self, mock_get):
        self._prepare_valid_jwks(mock_get)
        self._seed_user(role="user", email="db-only@example.com")
        token = self._create_token(extra={"email": "token@example.com"})
        response = self._me(token, admin_emails="db-only@example.com")
        self.assertEqual(response.json()["role"], "user")
        self.assertEqual(response.json()["email"], "token@example.com")

    @patch("app.dependencies.httpx.get")
    def test_admin_emails_matching_is_case_and_space_insensitive(self, mock_get):
        self._prepare_valid_jwks(mock_get)
        token = self._create_token(extra={"email": " Boss@Example.COM "})
        response = self._me(token, admin_emails=" other@example.com , BOSS@example.com ")
        self.assertEqual(response.json()["role"], "admin")

    @patch("app.dependencies.httpx.get")
    def test_current_behavior_no_source_can_demote_an_admin(self, mock_get):
        # Satu sumber admin sudah cukup; klaim 'user' di JWT tidak mencabutnya.
        # Diubah di gelombang b, P1-01.
        self._prepare_valid_jwks(mock_get)
        self._seed_user(role="admin")
        token = self._create_token(
            extra={"app_metadata": {"role": "user"}, "role": "authenticated"}
        )
        response = self._me(token, admin_emails="")
        self.assertEqual(response.json()["role"], "admin")

    @patch("app.dependencies.httpx.get")
    def test_current_behavior_nonstandard_users_role_is_returned_as_is(self, mock_get):
        # users.role selain 'admin'/'user' diteruskan apa adanya ke respons /auth/me.
        # Diubah di gelombang b, P1-05.
        self._prepare_valid_jwks(mock_get)
        self._seed_user(role="merchant")
        response = self._me(self._create_token())
        self.assertEqual(response.json()["role"], "merchant")
        self.assertEqual(self._put_photo(self._create_token()).status_code, 403)
