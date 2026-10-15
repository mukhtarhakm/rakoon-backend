import os
import sys
import uuid
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app
from app.database import Base, get_db
from app.dependencies import get_current_user, get_current_admin_user, get_current_user_profile
from app.models.db_models import Store, StoreOwner, StoreSubmission, User

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


MEDAN = {"latitude": 3.5952, "longitude": 98.6722}
YOGYA = (-7.7829, 110.4083)


def payload(**overrides):
    body = {
        "store_name": "Apotek Sehat Medan",
        "store_address": "Jl. Gatot Subroto No. 5, Medan",
        "applicant_name": "Ahmad",
        "applicant_phone": "0855667788",
        "applicant_role": "Pemilik",
        **MEDAN,
    }
    body.update(overrides)
    return body


class TestStoreSubmissionCoordinates(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self.user_id = str(uuid.uuid4())
        self.admin_id = str(uuid.uuid4())
        self.db.add_all([
            User(id=self.user_id, email="pemohon@example.com", nama="Pemohon", role="user"),
            User(id=self.admin_id, email="admin@example.com", nama="Admin", role="admin"),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)
        app.dependency_overrides.clear()

    def _login(self, admin=False):
        uid = self.admin_id if admin else self.user_id
        app.dependency_overrides[get_current_user] = lambda: uid
        app.dependency_overrides[get_current_user_profile] = lambda: {"id": uid, "role": "admin" if admin else "user", "email": "x@example.com"}
        if admin:
            app.dependency_overrides[get_current_admin_user] = lambda: {"user_id": uid, "role": "admin"}
        else:
            app.dependency_overrides.pop(get_current_admin_user, None)

    def _submit(self, **overrides):
        self._login()
        return self.client.post("/ads/submit-new-store", json=payload(**overrides))

    def _verify(self, sub_id, **body):
        self._login(admin=True)
        return self.client.post("/ads/verify-new-store", json={"submission_id": sub_id, **body})

    # ---- submit ----
    def test_coordinates_are_required(self):
        for missing in ("latitude", "longitude"):
            body = payload()
            body.pop(missing)
            self._login()
            res = self.client.post("/ads/submit-new-store", json=body)
            self.assertEqual(res.status_code, 422, missing)

    def test_out_of_range_coordinates_are_rejected(self):
        for bad in ({"latitude": 95}, {"latitude": -90.5}, {"longitude": 181}, {"longitude": -200}):
            self.assertEqual(self._submit(**bad).status_code, 422, bad)

    def test_valid_submission_stores_and_returns_coordinates_without_store_category(self):
        res = self._submit(store_category="Apotek")  # legacy client still sending it
        self.assertEqual(res.status_code, 201)
        body = res.json()
        self.assertEqual((body["latitude"], body["longitude"]), (3.5952, 98.6722))
        self.assertNotIn("store_category", body)

        row = self.db.query(StoreSubmission).one()
        self.assertEqual((row.latitude, row.longitude), (3.5952, 98.6722))
        self.assertIsNone(row.store_category)

    def test_reads_do_not_expose_store_category_and_show_coordinates(self):
        self._submit()
        mine = self.client.get("/ads/my-submissions").json()
        self.assertTrue(all("store_category" not in item for item in mine))
        self.assertEqual(mine[0]["latitude"], 3.5952)

        self._login(admin=True)
        pending = [p for p in self.client.get("/ads/pending-claims").json() if p["claim_type"] == "new_store"]
        self.assertEqual(len(pending), 1)
        self.assertNotIn("store_category", pending[0])
        self.assertEqual((pending[0]["latitude"], pending[0]["longitude"]), (3.5952, 98.6722))

    # ---- verify ----
    def test_verify_uses_the_applicant_coordinates_not_the_fixed_yogyakarta_point(self):
        sub_id = self._submit().json()["id"]
        res = self._verify(sub_id)
        self.assertEqual(res.status_code, 200)
        store = self.db.query(Store).filter(Store.id == res.json()["store_id"]).one()
        self.assertEqual((store.lat, store.lng), (3.5952, 98.6722))
        self.assertNotEqual((store.lat, store.lng), YOGYA)

    def test_admin_can_correct_the_coordinates_when_verifying(self):
        sub_id = self._submit().json()["id"]
        res = self._verify(sub_id, latitude=3.6, longitude=98.7)
        self.assertEqual(res.status_code, 200)
        store = self.db.query(Store).filter(Store.id == res.json()["store_id"]).one()
        self.assertEqual((store.lat, store.lng), (3.6, 98.7))

    def test_admin_correction_must_be_complete_and_in_range(self):
        sub_id = self._submit().json()["id"]
        for bad in ({"latitude": 3.6}, {"longitude": 98.7}, {"latitude": 91, "longitude": 98.7}, {"latitude": 3.6, "longitude": 181}):
            self.assertEqual(self._verify(sub_id, **bad).status_code, 422, bad)
        self.assertEqual(self.db.query(Store).count(), 0)

    def test_legacy_submission_without_coordinates_is_not_placed_at_a_default_point(self):
        sub_id = self._submit().json()["id"]
        row = self.db.query(StoreSubmission).one()
        row.latitude = None
        row.longitude = None
        self.db.commit()

        res = self._verify(sub_id)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.db.query(Store).count(), 0)
        self.assertEqual(self.db.query(StoreOwner).count(), 0)

        ok = self._verify(sub_id, latitude=3.6, longitude=98.7)
        self.assertEqual(ok.status_code, 200)


if __name__ == "__main__":
    unittest.main()
