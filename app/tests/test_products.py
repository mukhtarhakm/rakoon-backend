import unittest
import uuid
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
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

class TestProductsApi(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

        # Seed sample product
        prod = Product(
            id=str(uuid.uuid4()),
            nama="Indomie Goreng Spesial",
            kategori="Makanan Instan",
            ukuran=85.0,
            satuan="g"
        )
        self.db.add(prod)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)
        app.dependency_overrides.pop(get_db, None)

    def test_get_products_list(self):
        response = self.client.get("/products/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertGreaterEqual(len(data), 1)

    def test_get_products_with_search_and_category(self):
        response = self.client.get("/products/?search=indomie&category=Makanan Instan")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertGreaterEqual(len(data), 1)
        self.assertEqual(data[0]["nama"], "Indomie Goreng Spesial")

    def test_get_products_catalog(self):
        response = self.client.get("/products/catalog?limit=5")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertGreaterEqual(len(data), 1)

if __name__ == "__main__":
    unittest.main()
