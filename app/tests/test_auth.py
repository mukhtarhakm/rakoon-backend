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
        from app.dependencies import get_current_admin_user
        app.dependency_overrides.pop(get_current_admin_user, None)
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

    def _admin_gate(self):
        # POST /stores/ dan /price/ khusus admin. Test ini menguji identitas/validasi token, bukan gerbang admin
        # (lihat tests/test_admin_only_writes.py), jadi gerbangnya dilewati; identitas tetap dari get_current_user asli.
        from app.dependencies import get_current_admin_user
        app.dependency_overrides[get_current_admin_user] = lambda: {"user_id": "admin", "role": "admin"}

    def _make_auth_request(self, token=None, headers=None):
        self._admin_gate()
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
        jwks_cache.last_failure = 0.0
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
        self._admin_gate()

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
        self._admin_gate()

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
        self._admin_gate()

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
    # Perilaku target opsi B (audit/P1_fix.md): admin HANYA bila email ada di ADMIN_EMAILS
    # dan email_confirmed_at terisi lewat Supabase Admin API (di-mock, fail-closed).
    # users.role, app_metadata.role, dan klaim role tidak lagi memberi admin.
    # JWKS: gagal saat cache kosong -> 503, kunci lama dibatasi umurnya, refetch dibatasi.
    # ------------------------------------------------------------------

    ADMIN_EMAIL = "boss@example.com"
    CONFIRMED = {"email": "boss@example.com", "email_confirmed_at": "2026-01-01T00:00:00Z"}
    SERVICE_KEY = "test-service-key-not-real"

    def _reset_jwks_cache(self):
        from app.dependencies import jwks_cache
        jwks_cache.keys = {}
        jwks_cache.last_fetched = 0.0
        jwks_cache.last_failure = 0.0

    def _clear_admin_cache(self):
        import app.dependencies as deps
        getattr(deps, "_admin_confirm_cache", {}).clear()

    def _prepare_http(self, mock_get, admin=None, admin_exc=None, admin_status=200, jwks_exc=None, jwks_status=200):
        """Router HTTP palsu: JWKS dan Supabase Admin API. Tidak ada jaringan sungguhan."""
        import app.dependencies as deps
        self._reset_jwks_cache()
        self._clear_admin_cache()
        self.addCleanup(self._reset_jwks_cache)
        self.addCleanup(self._clear_admin_cache)
        self.admin_calls = []
        self.jwks_calls = []

        def router(url, *args, **kwargs):
            if url == deps.JWKS_URL:
                self.jwks_calls.append(url)
                if jwks_exc:
                    raise jwks_exc
                return MockResponse(self.jwks_data if jwks_status == 200 else {}, jwks_status)
            if "/auth/v1/admin/users/" in url:
                self.admin_calls.append({"url": url, "headers": kwargs.get("headers") or {}, "timeout": kwargs.get("timeout")})
                if admin_exc:
                    raise admin_exc
                return MockResponse(admin if admin is not None else {}, admin_status)
            raise AssertionError(f"panggilan HTTP tak terduga: {url}")

        mock_get.side_effect = router

    def _env(self, admin_emails="", service_key=None):
        # ADMIN_EMAILS dan kunci service selalu di-patch agar hasil tidak bergantung pada .env lokal.
        return patch.dict(os.environ, {
            "ADMIN_EMAILS": admin_emails,
            "SUPABASE_SERVICE_KEY": self.SERVICE_KEY if service_key is None else service_key,
        })

    def _me(self, token, admin_emails="", client=None, service_key=None):
        with self._env(admin_emails, service_key):
            return (client or self.client).get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    def _put_photo(self, token, admin_emails="", service_key=None):
        with self._env(admin_emails, service_key):
            return self.client.put(
                f"/products/{uuid.uuid4()}/photo",
                json={"foto_url": "https://example.com/a.png"},
                headers={"Authorization": f"Bearer {token}"},
            )

    def _get_protected(self, token):
        return self.client.get("/auth/test-protected", headers={"Authorization": f"Bearer {token}"})

    def _boss_token(self, **extra):
        claims = {"email": self.ADMIN_EMAIL}
        claims.update(extra)
        return self._create_token(extra=claims)

    # --- 1. Token valid tanpa klaim email -------------------------------------------------

    @patch("app.dependencies.httpx.get")
    def test_auth_me_and_test_protected_require_token(self, mock_get):
        self.assertEqual(self.client.get("/auth/me").status_code, 401)
        self.assertEqual(self.client.get("/auth/test-protected").status_code, 401)

    @patch("app.dependencies.httpx.get")
    def test_valid_token_without_email_claim_is_plain_user_and_never_calls_admin_api(self, mock_get):
        self._prepare_http(mock_get, admin=self.CONFIRMED)
        response = self._me(self._create_token(), admin_emails=self.ADMIN_EMAIL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"user_id": self.test_uuid, "email": None, "nama": None, "role": "user"},
        )
        self.assertEqual(self.admin_calls, [])

    # --- 2. Klaim role tidak lagi memberi admin ---------------------------------------------

    @patch("app.dependencies.httpx.get")
    def test_app_metadata_role_admin_no_longer_grants_admin(self, mock_get):
        self._prepare_http(mock_get)
        response = self._me(self._create_token(extra={"app_metadata": {"role": "admin"}}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "user")

    @patch("app.dependencies.httpx.get")
    def test_user_metadata_role_admin_is_not_honored_but_name_is_used(self, mock_get):
        self._prepare_http(mock_get)
        token = self._create_token(extra={"user_metadata": {"role": "admin", "full_name": "Budi"}})
        response = self._me(token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "user")
        self.assertEqual(response.json()["nama"], "Budi")

    @patch("app.dependencies.httpx.get")
    def test_top_level_role_claim_no_longer_grants_admin(self, mock_get):
        self._prepare_http(mock_get)
        for role in ("admin", "authenticated", "service_role"):
            with self.subTest(role=role):
                response = self._me(self._create_token(extra={"role": role}))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["role"], "user")

    @patch("app.dependencies.httpx.get")
    def test_null_user_metadata_and_app_metadata_do_not_crash(self, mock_get):
        # P1-07: dulu AttributeError -> 500. Sekarang token sah dengan klaim null tetap dilayani sebagai user.
        self._prepare_http(mock_get)
        client = TestClient(app, raise_server_exceptions=False)
        for extra in ({"user_metadata": None}, {"app_metadata": None}, {"user_metadata": None, "app_metadata": None}):
            with self.subTest(extra=extra):
                response = self._me(self._create_token(extra=extra), client=client)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["role"], "user")

    @patch("app.dependencies.httpx.get")
    def test_admin_endpoint_rejects_app_metadata_admin_claim_with_403(self, mock_get):
        self._prepare_http(mock_get)
        token = self._create_token(extra={"app_metadata": {"role": "admin"}})
        self.assertEqual(self._put_photo(token).status_code, 403)

    # --- 3. Prioritas: hanya ADMIN_EMAILS + email_confirmed_at ------------------------------------

    @patch("app.dependencies.httpx.get")
    def test_admin_when_email_in_admin_emails_and_confirmed(self, mock_get):
        self._prepare_http(mock_get, admin=self.CONFIRMED)
        response = self._me(self._boss_token(), admin_emails=self.ADMIN_EMAIL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "admin")
        self.assertEqual(response.json()["email"], self.ADMIN_EMAIL)

    @patch("app.dependencies.httpx.get")
    def test_admin_api_called_with_service_key_short_timeout_and_key_not_leaked(self, mock_get):
        import app.dependencies as deps
        self._prepare_http(mock_get, admin=self.CONFIRMED)
        response = self._me(self._boss_token(), admin_emails=self.ADMIN_EMAIL)
        self.assertEqual(len(self.admin_calls), 1)
        call = self.admin_calls[0]
        self.assertEqual(call["url"], f"{deps.base_url}/auth/v1/admin/users/{self.test_uuid}")
        self.assertEqual(call["headers"].get("apikey"), self.SERVICE_KEY)
        self.assertEqual(call["headers"].get("Authorization"), f"Bearer {self.SERVICE_KEY}")
        self.assertLessEqual(call["timeout"], 5.0)
        self.assertNotIn(self.SERVICE_KEY, response.text)

    @patch("app.dependencies.httpx.get")
    def test_non_admin_email_is_user_and_admin_api_is_not_called(self, mock_get):
        self._prepare_http(mock_get, admin=self.CONFIRMED)
        token = self._create_token(extra={"email": "someone@example.com"})
        response = self._me(token, admin_emails=self.ADMIN_EMAIL)
        self.assertEqual(response.json()["role"], "user")
        self.assertEqual(self.admin_calls, [])

    @patch("app.dependencies.httpx.get")
    def test_old_sources_app_metadata_and_role_claim_do_not_grant_admin(self, mock_get):
        # Regresi penghapusan sumber lama (P1-01/P1-05/P1-06): email tidak ada di ADMIN_EMAILS.
        self._prepare_http(mock_get, admin=self.CONFIRMED)
        token = self._create_token(
            extra={"email": "other@example.com", "app_metadata": {"role": "admin"}, "role": "admin"}
        )
        response = self._me(token, admin_emails=self.ADMIN_EMAIL)
        self.assertEqual(response.json()["role"], "user")
        self.assertEqual(self._put_photo(token, admin_emails=self.ADMIN_EMAIL).status_code, 403)

    @patch("app.dependencies.httpx.get")
    def test_empty_admin_emails_means_no_admin(self, mock_get):
        self._prepare_http(mock_get, admin=self.CONFIRMED)
        for value in ("", " ", ",,"):
            with self.subTest(admin_emails=value):
                self.assertEqual(self._me(self._boss_token(), admin_emails=value).json()["role"], "user")
        self.assertEqual(self.admin_calls, [])

    @patch("app.dependencies.httpx.get")
    def test_admin_emails_matching_is_case_and_space_insensitive(self, mock_get):
        self._prepare_http(mock_get, admin=self.CONFIRMED)
        token = self._create_token(extra={"email": " Boss@Example.COM "})
        response = self._me(token, admin_emails=" other@example.com , BOSS@example.com ")
        self.assertEqual(response.json()["role"], "admin")

    @patch("app.dependencies.httpx.get")
    def test_unconfirmed_email_is_not_admin(self, mock_get):
        for confirmed_at in (None, ""):
            with self.subTest(email_confirmed_at=confirmed_at):
                self._prepare_http(mock_get, admin={"email": self.ADMIN_EMAIL, "email_confirmed_at": confirmed_at})
                response = self._me(self._boss_token(), admin_emails=self.ADMIN_EMAIL)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["role"], "user")

    @patch("app.dependencies.httpx.get")
    def test_admin_api_failure_is_fail_closed_not_500(self, mock_get):
        client = TestClient(app, raise_server_exceptions=False)
        failures = [
            {"admin_exc": Exception("boom")},
            {"admin_exc": TimeoutError("timeout")},
            {"admin": {}, "admin_status": 500},
            {"admin": {}, "admin_status": 404},
            {"admin": ["not", "a", "dict"]},
            {"admin": {"email": "other@example.com", "email_confirmed_at": "2026-01-01T00:00:00Z"}},
        ]
        for kwargs in failures:
            with self.subTest(kwargs=str(kwargs)):
                self._prepare_http(mock_get, **kwargs)
                response = self._me(self._boss_token(), admin_emails=self.ADMIN_EMAIL, client=client)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["role"], "user")

    @patch("app.dependencies.httpx.get")
    def test_missing_service_key_is_fail_closed(self, mock_get):
        self._prepare_http(mock_get, admin=self.CONFIRMED)
        # "" lewat patch.dict menimpa nilai .env; tanpa kunci tidak ada panggilan Admin API.
        response = self._me(self._boss_token(), admin_emails=self.ADMIN_EMAIL, service_key=" ")
        self.assertEqual(response.json()["role"], "user")
        self.assertEqual(self.admin_calls, [])

    @patch("app.dependencies.httpx.get")
    def test_positive_admin_result_is_cached_briefly_and_negative_is_not(self, mock_get):
        self._prepare_http(mock_get, admin=self.CONFIRMED)
        for _ in range(3):
            self.assertEqual(self._me(self._boss_token(), admin_emails=self.ADMIN_EMAIL).json()["role"], "admin")
        self.assertEqual(len(self.admin_calls), 1)

        self._prepare_http(mock_get, admin={"email": self.ADMIN_EMAIL, "email_confirmed_at": None})
        for _ in range(2):
            self.assertEqual(self._me(self._boss_token(), admin_emails=self.ADMIN_EMAIL).json()["role"], "user")
        self.assertEqual(len(self.admin_calls), 2)

    @patch("app.dependencies.httpx.get")
    def test_admin_dependency_end_to_end_admin_passes_and_user_gets_403(self, mock_get):
        self._prepare_http(mock_get, admin=self.CONFIRMED)
        # Admin lolos gerbang (produk acak -> 404, bukan 403).
        self.assertEqual(self._put_photo(self._boss_token(), admin_emails=self.ADMIN_EMAIL).status_code, 404)
        # Pengguna biasa -> 403.
        self.assertEqual(self._put_photo(self._create_token(), admin_emails=self.ADMIN_EMAIL).status_code, 403)

    # --- 4. JWKS (P1-07/P1-08) -----------------------------------------------------------------------

    @patch("app.dependencies.httpx.get")
    def test_jwks_fetch_error_with_empty_cache_returns_503(self, mock_get):
        self._prepare_http(mock_get, jwks_exc=Exception("jwks down"))
        response = self._get_protected(self._create_token())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("jwks", response.text.lower())

    @patch("app.dependencies.httpx.get")
    def test_jwks_http_error_with_empty_cache_returns_503(self, mock_get):
        self._prepare_http(mock_get, jwks_status=503)
        self.assertEqual(self._get_protected(self._create_token()).status_code, 503)

    @patch("app.dependencies.httpx.get")
    def test_jwks_outage_does_not_refetch_on_every_request(self, mock_get):
        self._prepare_http(mock_get, jwks_exc=Exception("jwks down"))
        for _ in range(3):
            self.assertEqual(self._get_protected(self._create_token()).status_code, 503)
        self.assertEqual(len(self.jwks_calls), 1)

    @patch("app.dependencies.httpx.get")
    def test_stale_jwks_keys_valid_within_limit_rejected_beyond_it_when_refresh_fails(self, mock_get):
        from app.dependencies import jwks_cache
        self._prepare_http(mock_get, jwks_exc=Exception("jwks down"))
        jwks_cache.keys = {self.test_kid: self.jwks_data["keys"][0]}

        jwks_cache.last_fetched = time.time() - 1000  # lewat TTL (300 dtk), masih dalam batas umur
        ok = self._get_protected(self._create_token())
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json(), {"user_id": self.test_uuid})

        self._reset_jwks_cache()
        jwks_cache.keys = {self.test_kid: self.jwks_data["keys"][0]}
        jwks_cache.last_fetched = time.time() - 24 * 3600  # jauh melewati batas umur kunci lama
        self.assertEqual(self._get_protected(self._create_token()).status_code, 503)

    @patch("app.dependencies.httpx.get")
    def test_unknown_kid_does_not_trigger_unbounded_jwks_fetches(self, mock_get):
        self._prepare_http(mock_get)
        self.assertEqual(self._get_protected(self._create_token()).status_code, 200)
        self.assertEqual(len(self.jwks_calls), 1)
        for i in range(5):
            response = self._get_protected(self._create_token(kid=f"random-kid-{i}"))
            self.assertEqual(response.status_code, 401)
        self.assertEqual(len(self.jwks_calls), 1)

    @patch("app.dependencies.httpx.get")
    def test_unknown_kid_after_cache_age_triggers_a_single_refresh(self, mock_get):
        from app.dependencies import jwks_cache
        self._prepare_http(mock_get)
        self.assertEqual(self._get_protected(self._create_token()).status_code, 200)
        jwks_cache.last_fetched = time.time() - 120  # lewat jeda minimum refetch
        self.assertEqual(self._get_protected(self._create_token(kid="rotated-key")).status_code, 401)
        self.assertEqual(len(self.jwks_calls), 2)
        self.assertEqual(self._get_protected(self._create_token(kid="another-key")).status_code, 401)
        self.assertEqual(len(self.jwks_calls), 2)

    @patch("app.dependencies.httpx.get")
    def test_invalid_key_and_pyjwk_errors_return_401_not_500(self, mock_get):
        self._prepare_http(mock_get)
        client = TestClient(app, raise_server_exceptions=False)
        token = self._create_token()
        for exc in (jwt.InvalidKeyError("bad key"), jwt.PyJWKError("bad jwk")):
            with self.subTest(exc=type(exc).__name__):
                with patch("app.dependencies.jwt.decode", side_effect=exc):
                    response = client.get("/auth/test-protected", headers={"Authorization": f"Bearer {token}"})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["detail"], "Not authenticated")

    @patch("app.dependencies.httpx.get")
    def test_null_iat_claim_returns_401_not_500(self, mock_get):
        self._prepare_http(mock_get)
        client = TestClient(app, raise_server_exceptions=False)
        token = self._create_token(extra={"iat": None})
        response = client.get("/auth/test-protected", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)

    @patch("app.dependencies.httpx.get")
    def test_malformed_jwk_in_jwks_returns_401_not_500(self, mock_get):
        from app.dependencies import jwks_cache
        self._prepare_http(mock_get)
        client = TestClient(app, raise_server_exceptions=False)
        broken = dict(self.jwks_data["keys"][0])
        broken["x"] = "!!not-base64!!"
        jwks_cache.keys = {self.test_kid: broken}
        jwks_cache.last_fetched = time.time()
        response = client.get("/auth/test-protected", headers={"Authorization": f"Bearer {self._create_token()}"})
        self.assertEqual(response.status_code, 401)

    # --- 5. Token kedaluwarsa dan alg selain ES256 (perilaku tetap) ---------------------------------

    @patch("app.dependencies.httpx.get")
    def test_expired_token_within_leeway_is_accepted(self, mock_get):
        self._prepare_http(mock_get)
        token = self._create_token(exp=int(time.time()) - 30)
        self.assertEqual(self._get_protected(token).status_code, 200)

    @patch("app.dependencies.httpx.get")
    def test_alg_es384_rejected_before_jwks_fetch(self, mock_get):
        self._prepare_http(mock_get)
        token = self._create_token(alg="ES384", key=ec.generate_private_key(ec.SECP384R1()))
        self.assertEqual(self._get_protected(token).status_code, 401)
        self.assertEqual(self.jwks_calls, [])

    @patch("app.dependencies.httpx.get")
    def test_alg_none_rejected_before_jwks_fetch(self, mock_get):
        self._prepare_http(mock_get)
        payload = {
            "sub": self.test_uuid,
            "aud": "authenticated",
            "iss": self.expected_issuer,
            "exp": int(time.time()) + 3600,
        }
        token = jwt.encode(payload, None, algorithm="none", headers={"kid": self.test_kid})
        self.assertEqual(self._get_protected(token).status_code, 401)
        self.assertEqual(self.jwks_calls, [])

    # --- 6. /auth/test-protected hanya untuk non-produksi (P1-11) -------------------------------------

    @patch("app.dependencies.httpx.get")
    def test_test_protected_is_disabled_in_production(self, mock_get):
        self._prepare_http(mock_get)
        token = self._create_token()
        for env in (
            {"RAKOON_ENV": "production", "ENVIRONMENT": ""},
            {"RAKOON_ENV": "", "ENVIRONMENT": "production"},
            {"RAKOON_ENV": "PROD", "ENVIRONMENT": ""},
        ):
            with self.subTest(env=env), patch.dict(os.environ, env):
                self.assertEqual(self._get_protected(token).status_code, 404)
                self.assertEqual(self.client.get("/auth/test-protected").status_code, 404)  # tanpa token juga 404

    @patch("app.dependencies.httpx.get")
    def test_test_protected_works_outside_production(self, mock_get):
        self._prepare_http(mock_get)
        token = self._create_token()
        for env in (
            {"RAKOON_ENV": "", "ENVIRONMENT": ""},
            {"RAKOON_ENV": "development", "ENVIRONMENT": ""},
            {"RAKOON_ENV": "staging", "ENVIRONMENT": ""},
        ):
            with self.subTest(env=env), patch.dict(os.environ, env):
                response = self._get_protected(token)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"user_id": self.test_uuid})
                self.assertEqual(self.client.get("/auth/test-protected").status_code, 401)

    @patch("app.dependencies.httpx.get")
    def test_auth_me_stays_available_in_production(self, mock_get):
        self._prepare_http(mock_get)
        with patch.dict(os.environ, {"RAKOON_ENV": "production", "ENVIRONMENT": ""}):
            self.assertEqual(self._me(self._create_token()).status_code, 200)
