import os
import sys
import uuid
import unittest
from unittest import mock

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import dependencies as deps
from app.main import app
from app.database import Base, get_db
from app.dependencies import get_current_admin_user
from app.models.db_models import Store, StoreOwner, StoreSubmission

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


def admin_api_response(url, **kwargs):
    uid = url.rsplit("/", 1)[-1]
    return httpx.Response(
        200,
        json={"id": uid, "email": f"{uid[:8]}@example.com", "user_metadata": {"full_name": f"Nama {uid[:8]}"}},
        request=httpx.Request("GET", url),
    )


class TestPendingClaimsIdentity(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_admin_user] = lambda: {"user_id": "a", "role": "admin"}
        deps._identity_cache.clear()
        self.client = TestClient(app)
        self.claim_uid = str(uuid.uuid4())
        self.sub_uid = str(uuid.uuid4())
        store = Store(nama="Toko A", alamat="Jl. A", lat=-7.7, lng=110.3)
        self.db.add(store)
        self.db.commit()
        self.db.add(StoreOwner(user_id=self.claim_uid, store_id=store.id, status="pending"))
        self.db.add(StoreSubmission(
            user_id=self.sub_uid, store_name="Toko B", store_address="Jl. B",
            applicant_name="B", applicant_phone="08", applicant_role="Pemilik",
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)
        app.dependency_overrides.clear()

    def _get(self):
        return self.client.get("/ads/pending-claims")

    def test_nama_and_email_come_from_admin_api(self):
        with mock.patch.object(deps.httpx, "get", side_effect=admin_api_response) as get:
            res = self._get()
        self.assertEqual(res.status_code, 200)
        by_uid = {item["user_id"]: item for item in res.json()}
        for uid in (self.claim_uid, self.sub_uid):
            self.assertEqual(by_uid[uid]["user_email"], f"{uid[:8]}@example.com")
            self.assertEqual(by_uid[uid]["user_nama"], f"Nama {uid[:8]}")
        self.assertEqual(get.call_count, 2)

    def test_admin_api_failure_keeps_queue_200_with_empty_fields(self):
        with mock.patch.object(deps.httpx, "get", side_effect=httpx.ConnectTimeout("boom")):
            res = self._get()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 2)
        for item in res.json():
            self.assertIn(item["user_nama"], (None, "", "-"))
            self.assertIn(item["user_email"], (None, "", "-"))

    def test_duplicate_user_ids_are_fetched_once_and_result_is_cached(self):
        self.db.add(StoreSubmission(
            user_id=self.claim_uid, store_name="Toko C", store_address="Jl. C",
            applicant_name="C", applicant_phone="08", applicant_role="Pemilik",
        ))
        self.db.commit()
        with mock.patch.object(deps.httpx, "get", side_effect=admin_api_response) as get:
            self._get()
            self._get()
        self.assertEqual(get.call_count, 2)  # 2 unique ids, second request fully cached

    def test_lookups_are_capped(self):
        ids = [str(uuid.uuid4()) for _ in range(deps.IDENTITY_MAX_LOOKUPS + 10)]
        with mock.patch.object(deps.httpx, "get", side_effect=admin_api_response) as get:
            result = deps.get_user_identities(ids)
        self.assertEqual(get.call_count, deps.IDENTITY_MAX_LOOKUPS)
        self.assertEqual(len(result), deps.IDENTITY_MAX_LOOKUPS)

    def test_non_uuid_ids_are_never_sent_to_admin_api(self):
        with mock.patch.object(deps.httpx, "get", side_effect=admin_api_response) as get:
            result = deps.get_user_identities(["../../x", "not-a-uuid"])
        get.assert_not_called()
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
