"""GET /ads/home-banners: validasi rentang lat/lng/radius_km (422 bila di luar rentang)."""
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

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


class TestHomeBannersParams(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)

    def get(self, **params):
        return self.client.get("/ads/home-banners", params=params)

    def test_out_of_range_values_are_422(self):
        bad = [
            {"lat": 90.01}, {"lat": -90.01}, {"lat": 1000},
            {"lng": 180.01}, {"lng": -180.01}, {"lng": 9999},
            {"radius_km": 0}, {"radius_km": -1}, {"radius_km": 100.01}, {"radius_km": 1e9},
            {"lat": "nan"}, {"radius_km": "inf"},
        ]
        for params in bad:
            self.assertEqual(self.get(**params).status_code, 422, params)

    def test_boundary_values_are_accepted(self):
        good = [
            {"lat": 90, "lng": 180, "radius_km": 100},
            {"lat": -90, "lng": -180, "radius_km": 0.1},
            {},
        ]
        for params in good:
            res = self.get(**params)
            self.assertEqual(res.status_code, 200, params)
            self.assertEqual(res.json(), [])


if __name__ == "__main__":
    unittest.main()
