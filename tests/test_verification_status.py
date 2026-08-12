import unittest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.models.db_models import Product, Store, PriceEntry
from app.models.schemas import VerificationStatus

SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class TestVerificationStatus(unittest.TestCase):

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        self.original_get_db = app.dependency_overrides.get(get_db)
        app.dependency_overrides[get_db] = lambda: self.db
        from app.dependencies import get_current_user
        app.dependency_overrides[get_current_user] = lambda: self.user_id
        self.client = TestClient(app)

        # Seed initial data
        self.store_id = "a5b07384-d113-4956-b51c-43f11075d654"
        self.user_id = "b5b07384-d113-4956-b51c-43f11075d655"
        self.prod_id = "c5b07384-d113-4956-b51c-43f11075d654"

        store = Store(id=self.store_id, nama="Indomaret Sudirman", lat=-6.2088, lng=106.8456)
        product = Product(id=self.prod_id, nama="Ultra Milk Cokelat 1L", kategori="Minuman", ukuran=1.0, satuan="L")
        
        self.db.add_all([store, product])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)
        from app.dependencies import get_current_user
        app.dependency_overrides.pop(get_current_user, None)
        if self.original_get_db is not None:
            app.dependency_overrides[get_db] = self.original_get_db
        else:
            app.dependency_overrides.pop(get_db, None)

    def test_scan_confirm_saves_as_verified(self):
        payload = {
            "store_id": self.store_id,
            "user_id": self.user_id,
            "items": [
                {
                    "nama_produk": "Ultra Milk Cokelat 1L",
                    "harga": 20000,
                    "ukuran": 1.0,
                    "satuan": "L",
                    "kategori": "Minuman"
                }
            ]
        }
        response = self.client.post("/scan/confirm", json=payload)
        self.assertEqual(response.status_code, 201)
        
        # Check stored status in database
        db_entries = self.db.query(PriceEntry).all()
        self.assertEqual(len(db_entries), 1)
        self.assertEqual(db_entries[0].status_verifikasi, VerificationStatus.VERIFIED)

    def test_manual_price_saves_as_verified(self):
        payload = {
            "product_id": self.prod_id,
            "store_id": self.store_id,
            "harga": 18500,
            "sumber_user_id": self.user_id
        }
        response = self.client.post("/price/", json=payload)
        self.assertEqual(response.status_code, 201)
        
        # Check stored status in database
        db_entries = self.db.query(PriceEntry).all()
        self.assertEqual(len(db_entries), 1)
        self.assertEqual(db_entries[0].status_verifikasi, VerificationStatus.VERIFIED)

    def test_client_cannot_control_verification_status(self):
        # 1. Manual Price route injection attempt
        payload_price = {
            "product_id": self.prod_id,
            "store_id": self.store_id,
            "harga": 18500,
            "sumber_user_id": self.user_id,
            "status_verifikasi": "rejected"  # Attempting to inject custom status
        }
        response = self.client.post("/price/", json=payload_price)
        self.assertEqual(response.status_code, 201)
        
        # Re-fetch entry
        entry = self.db.query(PriceEntry).first()
        self.assertEqual(entry.status_verifikasi, VerificationStatus.VERIFIED) # Stored status must be verified, ignoring client input
        self.db.delete(entry)
        self.db.commit()

        # 2. Scan Confirm route injection attempt
        payload_scan = {
            "store_id": self.store_id,
            "user_id": self.user_id,
            "items": [
                {
                    "nama_produk": "Ultra Milk Cokelat 1L",
                    "harga": 20000,
                    "ukuran": 1.0,
                    "satuan": "L",
                    "kategori": "Minuman",
                    "status_verifikasi": "rejected"  # Attempting to inject custom status
                }
            ]
        }
        response = self.client.post("/scan/confirm", json=payload_scan)
        self.assertEqual(response.status_code, 201)
        
        entry = self.db.query(PriceEntry).first()
        self.assertEqual(entry.status_verifikasi, VerificationStatus.VERIFIED)

    def test_price_history_excludes_rejected(self):
        # Seed test data with different statuses
        p_verified = PriceEntry(product_id=self.prod_id, store_id=self.store_id, harga=10000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED)
        p_pending = PriceEntry(product_id=self.prod_id, store_id=self.store_id, harga=11000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.PENDING)
        p_rejected = PriceEntry(product_id=self.prod_id, store_id=self.store_id, harga=5000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.REJECTED)
        
        self.db.add_all([p_verified, p_pending, p_rejected])
        self.db.commit()

        response = self.client.get(f"/price/api/v1/products/{self.prod_id}/price-history")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        # Excluded rejected from list
        items = data["items"]
        self.assertEqual(len(items), 2)
        hargas = [item["harga"] for item in items]
        self.assertIn(10000, hargas)
        self.assertIn(11000, hargas)
        self.assertNotIn(5000, hargas)

        # Excluded rejected from trend aggregation
        trend = data["trend"]
        for pt in trend:
            self.assertNotEqual(pt["price"], 5000)

    def test_nearby_price_comparison_excludes_rejected(self):
        # We need a couple of stores to compare
        store_b_id = "a5b07384-d113-4956-b51c-43f11075d656"
        store_c_id = "a5b07384-d113-4956-b51c-43f11075d657"
        
        store_b = Store(id=store_b_id, nama="Alfamart Gatsu", lat=-6.2090, lng=106.8460)
        store_c = Store(id=store_c_id, nama="Superindo", lat=-6.2085, lng=106.8450)
        self.db.add_all([store_b, store_c])
        self.db.commit()

        # Seed prices:
        # Store A (self.store_id): Verified price = 10000
        p_a = PriceEntry(product_id=self.prod_id, store_id=self.store_id, harga=10000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.VERIFIED)
        # Store B: Rejected price = 5000
        p_b = PriceEntry(product_id=self.prod_id, store_id=store_b_id, harga=5000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.REJECTED)
        # Store C: Pending price = 11000
        p_c = PriceEntry(product_id=self.prod_id, store_id=store_c_id, harga=11000, sumber_user_id=self.user_id, status_verifikasi=VerificationStatus.PENDING)

        self.db.add_all([p_a, p_b, p_c])
        self.db.commit()

        response = self.client.get(f"/price/compare/{self.prod_id}?lat=-6.2088&lng=106.8456&radius_km=5")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        comparison = data["comparison"]
        
        # We expect Store B (with rejected price) to have no price data (or message) and not be listed with 5000.
        # Let's inspect the results.
        store_b_comparison = next((item for item in comparison if item["nama_toko"] == "Alfamart Gatsu"), None)
        self.assertIsNotNone(store_b_comparison)
        self.assertIsNone(store_b_comparison["harga_terbaru"])
        self.assertEqual(store_b_comparison["pesan"], "Belum ada data untuk produk ini di toko ini")

        # Store A and Store C must have prices
        store_a_comparison = next((item for item in comparison if item["nama_toko"] == "Indomaret Sudirman"), None)
        store_c_comparison = next((item for item in comparison if item["nama_toko"] == "Superindo"), None)
        self.assertEqual(store_a_comparison["harga_terbaru"], 10000)
        self.assertEqual(store_c_comparison["harga_terbaru"], 11000)

        # "Termurah" should be Indomaret Sudirman (10000), not Alfamart Gatsu (5000 is rejected)
        self.assertEqual(comparison[0]["nama_toko"], "Indomaret Sudirman")
        self.assertEqual(comparison[0]["harga_terbaru"], 10000)
