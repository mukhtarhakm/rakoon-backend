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
from app.models.db_models import Product, Store
from app.routers.recommendation import RECOMMENDED_MAX_LIMIT
from app.routers.stores import MAX_SEARCH_RADIUS_KM

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


class TestSpatialParamBounds(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self.product = Product(nama="Produk Batas", kategori="Minuman", ukuran=1.0, satuan="l")
        self.db.add_all([self.product, Store(nama="Toko Batas", lat=-7.78, lng=110.4)])
        self.db.commit()

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _urls(self, query):
        return {
            "recommended-products": f"/recommendation/recommended-products?{query}",
            "recommended alias": f"/recommendation/recommended?{query}",
            "stores nearby": f"/stores/nearby?{query}",
            "price compare": f"/price/compare/{self.product.id}?{query}",
        }

    def _assert_all_422(self, query, endpoints=None):
        for name, url in self._urls(query).items():
            if endpoints and name not in endpoints:
                continue
            with self.subTest(endpoint=name, query=query):
                self.assertEqual(self.client.get(url).status_code, 422)

    # --- koordinat ---

    def test_latitude_outside_minus_90_to_90_is_rejected(self):
        for lat in (90.0001, -90.0001, 1000, "nan", "inf", "-inf", "abc"):
            self._assert_all_422(f"lat={lat}&lng=110.4")

    def test_longitude_outside_minus_180_to_180_is_rejected(self):
        for lng in (180.0001, -180.0001, 1000, "nan", "inf", "abc"):
            self._assert_all_422(f"lat=-7.78&lng={lng}")

    def test_coordinates_exactly_on_the_boundary_are_accepted(self):
        for lat, lng in ((90, 180), (-90, -180), (0, 0)):
            for name, url in self._urls(f"lat={lat}&lng={lng}&radius_km=1").items():
                with self.subTest(endpoint=name, lat=lat, lng=lng):
                    self.assertEqual(self.client.get(url).status_code, 200)

    # --- radius ---

    def test_radius_must_be_positive_and_not_above_the_maximum(self):
        for radius in (0, -1, MAX_SEARCH_RADIUS_KM + 0.001, 100000, "nan", "inf"):
            self._assert_all_422(f"lat=-7.78&lng=110.4&radius_km={radius}")

    def test_radius_at_the_maximum_is_accepted(self):
        for name, url in self._urls(f"lat=-7.78&lng=110.4&radius_km={MAX_SEARCH_RADIUS_KM}").items():
            with self.subTest(endpoint=name):
                self.assertEqual(self.client.get(url).status_code, 200)

    # --- limit rekomendasi ---

    def test_recommended_limit_must_be_between_one_and_the_maximum(self):
        for limit in (0, -1, RECOMMENDED_MAX_LIMIT + 1, 1000000):
            self._assert_all_422(f"limit={limit}", endpoints=("recommended-products", "recommended alias"))
        for limit in (1, RECOMMENDED_MAX_LIMIT):
            for name in ("recommended-products", "recommended alias"):
                with self.subTest(endpoint=name, limit=limit):
                    self.assertEqual(self.client.get(self._urls(f"limit={limit}")[name]).status_code, 200)

    def test_recommended_without_coordinates_still_works(self):
        self.assertEqual(self.client.get("/recommendation/recommended-products").status_code, 200)

    def test_validation_error_body_does_not_echo_raw_input_and_is_json_safe(self):
        res = self.client.get("/recommendation/recommended-products?lat=nan&lng=110.4")
        self.assertEqual(res.status_code, 422)
        self.assertNotIn("input", res.text)


if __name__ == "__main__":
    unittest.main()
