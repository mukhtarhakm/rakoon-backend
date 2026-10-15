"""Alasan penolakan wajib (3..500 karakter setelah trim) untuk reject-claim dan reject-new-store."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.dependencies import get_current_user, get_current_admin_user
from app.models.db_models import Store, StoreOwner, StoreSubmission
from claim_evidence import evidence_for

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


USER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ADMIN = "cccccccc-cccc-cccc-cccc-cccccccccccc"
BAD_REASONS = [None, "", "   ", "\n\t ", "ab", "  a  ", "x" * 501, "  " + "x" * 501 + "  "]


class TestRejectReasonRequired(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: USER
        app.dependency_overrides[get_current_admin_user] = lambda: {"user_id": ADMIN, "role": "admin"}
        self.client = TestClient(app)
        self.store = Store(nama="Toko A", alamat="Jl. A No. 1", lat=-7.7, lng=110.4)
        self.db.add(self.store)
        self.db.commit()

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _claim(self):
        res = self.client.post("/ads/claim-store", json={"store_id": str(self.store.id), **evidence_for(USER)})
        self.assertEqual(res.status_code, 200)

    def _submission(self):
        sub = StoreSubmission(
            user_id=USER, store_name="Warung Baru", store_address="Jl. Baru 1", latitude=-7.7, longitude=110.4,
            applicant_name="Budi", applicant_phone="081234567890", applicant_role="owner", status="pending",
        )
        self.db.add(sub)
        self.db.commit()
        return str(sub.id)

    def _claim_body(self, **extra):
        return {"user_id": USER, "store_id": str(self.store.id), **extra}

    def test_reject_claim_refuses_missing_blank_short_or_long_reason(self):
        self._claim()
        for reason in BAD_REASONS:
            body = self._claim_body() if reason is None else self._claim_body(reason=reason)
            self.assertEqual(self.client.post("/ads/reject-claim", json=body).status_code, 422, repr(reason))
        self.db.expire_all()
        self.assertEqual(self.db.query(StoreOwner).one().status, "pending")

    def test_reject_claim_stores_the_trimmed_reason(self):
        self._claim()
        res = self.client.post("/ads/reject-claim", json=self._claim_body(reason="  Dokumen buram  "))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["rejection_reason"], "Dokumen buram")
        self.db.expire_all()
        self.assertEqual(self.db.query(StoreOwner).one().rejection_reason, "Dokumen buram")

    def test_reject_claim_accepts_the_length_boundaries(self):
        for reason in ("abc", "x" * 500):
            self.db.query(StoreOwner).delete()
            self.db.commit()
            self._claim()
            res = self.client.post("/ads/reject-claim", json=self._claim_body(reason=reason))
            self.assertEqual(res.status_code, 200, len(reason))

    def test_reject_new_store_refuses_missing_blank_short_or_long_reason(self):
        sub_id = self._submission()
        for reason in BAD_REASONS:
            body = {"submission_id": sub_id} if reason is None else {"submission_id": sub_id, "reason": reason}
            self.assertEqual(self.client.post("/ads/reject-new-store", json=body).status_code, 422, repr(reason))
        self.db.expire_all()
        self.assertEqual(self.db.query(StoreSubmission).one().status, "pending")

    def test_reject_new_store_stores_the_trimmed_reason(self):
        sub_id = self._submission()
        res = self.client.post("/ads/reject-new-store", json={"submission_id": sub_id, "reason": "  Alamat palsu  "})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["rejection_reason"], "Alamat palsu")
        self.db.expire_all()
        self.assertEqual(self.db.query(StoreSubmission).one().rejection_reason, "Alamat palsu")


if __name__ == "__main__":
    unittest.main()
