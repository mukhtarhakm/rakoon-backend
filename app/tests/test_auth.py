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

    def _create_token(self, sub="DEFAULT", aud="authenticated", iss="DEFAULT", exp="DEFAULT", kid="DEFAULT", alg="ES256", key=None, iat="DEFAULT"):
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
