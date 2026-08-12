import unittest
import os
import sys

# Ensure backend directory is in the path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.database import Base, get_db
from app.models.db_models import Store, Product, PriceEntry

# In-memory SQLite for testing
SQLALCHEMY_DATABASE_URL = "sqlite:///./test_rakoon.db"

engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def override_get_db():
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db

class TestStores(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
        if os.path.exists("./test_rakoon.db"):
            os.remove("./test_rakoon.db")

    def setUp(self):
        # Clean the tables before each test
        db = TestingSessionLocal()
        db.query(PriceEntry).delete()
        db.query(Product).delete()
        db.query(Store).delete()
        db.commit()
        db.close()

    def test_create_store_success(self):
        response = self.client.post(
            "/stores/",
            json={
                "nama": "Toko Sejahtera",
                "lat": -6.2088,
                "lng": 106.8456,
                "alamat": "Jl. Merdeka No. 10"
            }
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertIn("store_id", data)
        self.assertTrue(len(data["store_id"]) > 0)

    def test_create_store_invalid_name(self):
        response = self.client.post(
            "/stores/",
            json={
                "nama": "   ",
                "lat": -6.2088,
                "lng": 106.8456
            }
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Nama toko tidak boleh kosong.")

    def test_create_store_invalid_coordinates(self):
        # Latitude invalid (> 90)
        response = self.client.post(
            "/stores/",
            json={
                "nama": "Toko Sejahtera",
                "lat": 95.0,
                "lng": 106.8456
            }
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Latitude harus berada dalam rentang", response.json()["detail"])
        
        # Longitude invalid (< -180)
        response = self.client.post(
            "/stores/",
            json={
                "nama": "Toko Sejahtera",
                "lat": -6.2088,
                "lng": -190.0
            }
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Longitude harus berada dalam rentang", response.json()["detail"])

    def test_get_nearby_stores_empty(self):
        response = self.client.get("/stores/nearby?lat=-6.2088&lng=106.8456&radius_km=5")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["stores"], [])
        self.assertEqual(data["message"], "Data toko di sekitar masih terbatas")

    def test_get_nearby_stores_success(self):
        # Insert test stores
        db = TestingSessionLocal()
        # Close-by store (~1 km away)
        store1 = Store(id="a5b07384-d113-4956-b51c-43f11075d654", nama="Toko Dekat", lat=-6.2088, lng=106.8546, alamat="Dekat")
        # Far store (~10 km away)
        store2 = Store(id="a5b07384-d113-4956-b51c-43f11075d655", nama="Toko Jauh", lat=-6.2088, lng=106.9356, alamat="Jauh")
        db.add(store1)
        db.add(store2)
        db.commit()
        db.close()

        # Query with 5km radius
        response = self.client.get("/stores/nearby?lat=-6.2088&lng=106.8456&radius_km=5")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        # Should only find Toko Dekat
        self.assertEqual(len(data["stores"]), 1)
        self.assertEqual(data["stores"][0]["nama"], "Toko Dekat")
        self.assertEqual(data["message"], "Data toko di sekitar masih terbatas")

        # Add another close-by store to have 2 or more stores
        db = TestingSessionLocal()
        store3 = Store(id="a5b07384-d113-4956-b51c-43f11075d656", nama="Toko Dekat 2", lat=-6.2100, lng=106.8450, alamat="Dekat 2")
        db.add(store3)
        db.commit()
        db.close()

        response = self.client.get("/stores/nearby?lat=-6.2088&lng=106.8456&radius_km=5")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        # Should find both close-by stores, sorted by distance
        self.assertEqual(len(data["stores"]), 2)
        self.assertIsNone(data["message"])

    def test_price_comparison_integration(self):
        from app.models.db_models import Product, PriceEntry
        # Insert a product and a store and price entry
        db = TestingSessionLocal()
        product = Product(id="d3b07384-d113-4956-b51c-43f11075d654", nama="Susu UHT", kategori="Minuman", ukuran=1000.0, satuan="ml")
        store1 = Store(id="a5b07384-d113-4956-b51c-43f11075d654", nama="Toko Dekat", lat=-6.2088, lng=106.8546, alamat="Dekat")
        db.add(product)
        db.add(store1)
        db.commit()

        # Add price entry
        price_entry = PriceEntry(
            id="e5b07384-d113-4956-b51c-43f11075d654",
            product_id="d3b07384-d113-4956-b51c-43f11075d654",
            store_id="a5b07384-d113-4956-b51c-43f11075d654",
            harga=15000,
            sumber_user_id="b5b07384-d113-4956-b51c-43f11075d654",
            status_verifikasi="pending"
        )
        db.add(price_entry)
        db.commit()
        db.close()

        # Query comparison
        response = self.client.get("/price/compare/d3b07384-d113-4956-b51c-43f11075d654?lat=-6.2088&lng=106.8456&radius_km=5")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["product_id"], "d3b07384-d113-4956-b51c-43f11075d654")
        self.assertEqual(data["nama_produk"], "Susu UHT")
        self.assertEqual(len(data["comparison"]), 1)
        self.assertEqual(data["comparison"][0]["nama_toko"], "Toko Dekat")
        self.assertEqual(data["comparison"][0]["harga_terbaru"], 15000)

    def test_confirm_scan_results(self):
        # 1. Insert store & an existing product
        db = TestingSessionLocal()
        store = Store(id="a5b07384-d113-4956-b51c-43f11075d654", nama="Toko Uji", lat=-6.2088, lng=106.8456)
        existing_product = Product(id="c5b07384-d113-4956-b51c-43f11075d654", nama="Ultra Milk Rasa Coklat", kategori="General", ukuran=1000.0, satuan="ml")
        db.add(store)
        db.add(existing_product)
        db.commit()
        db.close()

        # 2. Call /scan/confirm to add one existing product price and one new product price
        payload = {
            "store_id": "a5b07384-d113-4956-b51c-43f11075d654",
            "user_id": "b5b07384-d113-4956-b51c-43f11075d655",
            "items": [
                {
                    "nama_produk": "Ultra Milk Rasa Coklat",
                    "harga": 22500,
                    "ukuran": 1000.0,
                    "satuan": "ml"
                },
                {
                    "nama_produk": "Ultra Milk Fresh Milk Biru",
                    "harga": 26200,
                    "ukuran": 1000.0,
                    "satuan": "ml"
                }
            ]
        }

        response = self.client.post("/scan/confirm", json=payload)
        self.assertEqual(response.status_code, 201)
        data = response.json()
        
        self.assertEqual(data["items_saved"], 2)
        self.assertEqual(data["products_created"], 1) # Only "Ultra Milk Fresh Milk Biru" should be created
        
        # Verify the new product has been saved in the DB with a non-null, valid UUID id
        db = TestingSessionLocal()
        new_prod = db.query(Product).filter(Product.nama == "Ultra Milk Fresh Milk Biru").first()
        self.assertIsNotNone(new_prod)
        self.assertIsNotNone(new_prod.id)
        self.assertNotEqual(new_prod.id, "")
        self.assertNotEqual(str(new_prod.id), "c5b07384-d113-4956-b51c-43f11075d654")
        
        # Verify the price entries have been saved
        price_entries = db.query(PriceEntry).all()
        self.assertEqual(len(price_entries), 2)
        db.close()

