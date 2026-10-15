"""Claim flow: mandatory evidence, caller-owned paths, one active claim per (user, store),
history never overwritten, audit fields, my-store gated by claim status, safer verify-new-store."""
import os
import sys
import uuid
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.dependencies import get_current_user, get_current_admin_user
from app.models.db_models import AdCampaign, Store, StoreOwner, StoreSubmission
from claim_evidence import evidence_for

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


USER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
ADMIN = "cccccccc-cccc-cccc-cccc-cccccccccccc"


class Base_(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self.store_a = Store(nama="Toko A", alamat="Jl. A No. 1", lat=-7.7, lng=110.4)
        self.store_b = Store(nama="Toko B", alamat="Jl. B No. 2", lat=-7.8, lng=110.5)
        self.db.add_all([self.store_a, self.store_b])
        self.db.commit()
        self.login(USER)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def login(self, user_id, admin=False):
        app.dependency_overrides[get_current_user] = lambda: user_id
        if admin:
            app.dependency_overrides[get_current_admin_user] = lambda: {"user_id": user_id, "role": "admin"}
        else:
            app.dependency_overrides.pop(get_current_admin_user, None)

    def claim(self, store=None, user=None, **overrides):
        body = {"store_id": str((store or self.store_a).id), **evidence_for(user or USER)}
        body.update(overrides)
        return self.client.post("/ads/claim-store", json=body)

    def owners(self, user=USER):
        self.db.expire_all()
        return self.db.query(StoreOwner).filter(StoreOwner.user_id == user).order_by(StoreOwner.created_at).all()


class TestEvidenceIsMandatoryAndOwned(Base_):
    def test_store_id_alone_is_not_enough(self):
        res = self.client.post("/ads/claim-store", json={"store_id": str(self.store_a.id)})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(self.owners(), [])

    def test_each_evidence_field_is_required_and_blank_is_refused(self):
        for field in ("applicant_name", "applicant_phone", "document_url", "store_photo_url"):
            body = {"store_id": str(self.store_a.id), **evidence_for(USER)}
            body.pop(field)
            self.assertEqual(self.client.post("/ads/claim-store", json=body).status_code, 422, field)
            body[field] = "   "
            self.assertIn(self.client.post("/ads/claim-store", json=body).status_code, (400, 422), field)
        self.assertEqual(self.owners(), [])

    def test_paths_must_belong_to_the_caller_and_to_the_right_kind(self):
        theirs = evidence_for(OTHER)
        mine = evidence_for(USER)
        cases = {
            "other user's document": {"document_url": theirs["document_url"]},
            "other user's photo": {"store_photo_url": theirs["store_photo_url"]},
            "photo path as document": {"document_url": mine["store_photo_url"]},
            "document path as photo": {"store_photo_url": mine["document_url"]},
            "legacy url": {"document_url": "/ads/documents/doc_legalitas_nib.pdf"},
            "external url": {"store_photo_url": "https://attacker.example.com/x.jpg"},
            "forged path": {"document_url": "documents/" + "a" * 48 + ".pdf"},
        }
        for label, override in cases.items():
            res = self.claim(**override)
            self.assertEqual(res.status_code, 400, label)
        self.assertEqual(self.owners(), [])

    def test_valid_evidence_creates_a_pending_claim_with_the_stored_paths(self):
        evidence = evidence_for(USER)
        res = self.claim(**evidence)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["claim_status"], "pending")
        row = self.owners()[0]
        self.assertEqual((row.document_url, row.store_photo_url), (evidence["document_url"], evidence["store_photo_url"]))
        self.assertEqual(row.applicant_name, "Budi Santoso")


class TestOneActiveClaimAndHistory(Base_):
    def test_second_claim_on_the_same_store_is_refused_and_changes_nothing(self):
        self.assertEqual(self.claim().status_code, 200)
        first = self.owners()[0]
        res = self.claim(applicant_name="Orang Lain")
        self.assertEqual(res.status_code, 409)
        rows = self.owners()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].applicant_name, first.applicant_name)
        self.assertEqual(rows[0].document_url, first.document_url)

    def test_reclaim_after_rejection_keeps_the_rejected_row_as_history(self):
        self.claim()
        self.login(ADMIN, admin=True)
        rej = self.client.post("/ads/reject-claim", json={"user_id": USER, "store_id": str(self.store_a.id), "reason": "Dokumen buram"})
        self.assertEqual(rej.status_code, 200)

        self.login(USER)
        self.assertEqual(self.claim().status_code, 200)
        rows = self.owners()
        self.assertEqual([r.status for r in rows], ["rejected", "pending"])
        self.assertEqual(rows[0].rejection_reason, "Dokumen buram")
        self.assertEqual(rows[0].reviewed_by, ADMIN)

        mine = self.client.get("/ads/my-store").json()
        self.assertEqual(mine["claim_status"], "pending")
        self.assertIsNone(mine["rejection_reason"])

    def test_a_pending_claim_on_another_store_does_not_overwrite_the_first(self):
        self.claim(store=self.store_a)
        self.assertEqual(self.claim(store=self.store_b).status_code, 200)
        rows = self.owners()
        self.assertEqual({str(r.store_id) for r in rows}, {str(self.store_a.id), str(self.store_b.id)})
        self.assertTrue(all(r.status == "pending" for r in rows))

    def test_store_with_a_verified_owner_cannot_be_claimed_by_someone_else(self):
        self.claim()
        self.login(ADMIN, admin=True)
        self.client.post("/ads/verify-claim", json={"user_id": USER, "store_id": str(self.store_a.id)})
        self.login(OTHER)
        self.assertEqual(self.claim(user=OTHER).status_code, 409)

    def test_verified_owner_cannot_claim_another_store(self):
        self.claim()
        self.login(ADMIN, admin=True)
        self.client.post("/ads/verify-claim", json={"user_id": USER, "store_id": str(self.store_a.id)})
        self.login(USER)
        self.assertEqual(self.claim(store=self.store_b).status_code, 409)


class TestMyStoreRespectsClaimStatus(Base_):
    def _paid_campaign(self):
        now = datetime.now(timezone.utc)
        self.db.add(AdCampaign(
            store_id=self.store_a.id, owner_user_id=OTHER, title="Promo", banner_url="https://example.com/b.jpg",
            duration_days=7, price_paid=30000, expires_at=now + timedelta(days=5), status="active",
            payment_status="paid", payment_ref="inv-secret-123",
        ))
        self.db.commit()

    def test_pending_claim_sees_no_campaigns_or_payment_data(self):
        self._paid_campaign()
        self.claim()
        res = self.client.get("/ads/my-store")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["campaigns"], [])
        self.assertNotIn("inv-secret-123", res.text)

    def test_rejected_claim_sees_no_campaigns(self):
        self._paid_campaign()
        self.claim()
        self.login(ADMIN, admin=True)
        self.client.post("/ads/reject-claim", json={"user_id": USER, "store_id": str(self.store_a.id), "reason": "x"})
        self.login(USER)
        res = self.client.get("/ads/my-store").json()
        self.assertEqual(res["claim_status"], "rejected")
        self.assertEqual(res["campaigns"], [])

    def test_verified_owner_sees_the_store_campaigns(self):
        self._paid_campaign()
        self.claim()
        self.login(ADMIN, admin=True)
        self.client.post("/ads/verify-claim", json={"user_id": USER, "store_id": str(self.store_a.id)})
        self.login(USER)
        res = self.client.get("/ads/my-store").json()
        self.assertTrue(res["is_claimed"])
        self.assertEqual(len(res["campaigns"]), 1)


class TestReviewAudit(Base_):
    def test_reject_records_reviewer_time_and_reason(self):
        self.claim()
        self.login(ADMIN, admin=True)
        self.client.post("/ads/reject-claim", json={"user_id": USER, "store_id": str(self.store_a.id), "reason": "Bukti tidak sesuai"})
        row = self.owners()[0]
        self.assertEqual(row.status, "rejected")
        self.assertEqual(row.reviewed_by, ADMIN)
        self.assertIsNotNone(row.reviewed_at)
        self.assertEqual(row.rejection_reason, "Bukti tidak sesuai")

    def test_verify_records_reviewer_and_time_and_audits_the_auto_rejected_competitor(self):
        self.claim()
        self.login(OTHER)
        self.claim(user=OTHER)
        self.login(ADMIN, admin=True)
        res = self.client.post("/ads/verify-claim", json={"user_id": USER, "store_id": str(self.store_a.id)})
        self.assertEqual(res.status_code, 200)

        winner, loser = self.owners(USER)[0], self.owners(OTHER)[0]
        self.assertEqual((winner.status, winner.reviewed_by), ("verified", ADMIN))
        self.assertIsNotNone(winner.reviewed_at)
        self.assertEqual((loser.status, loser.reviewed_by), ("rejected", ADMIN))
        self.assertIsNotNone(loser.reviewed_at)
        self.assertTrue(loser.rejection_reason)


class TestVerifyNewStoreIsSafer(Base_):
    def _submission(self, user, name="Warung Baru", address="Jl. Baru No. 9, Medan", **extra):
        sub = StoreSubmission(
            user_id=user, store_name=name, store_address=address, applicant_name="Ani", applicant_phone="081200000000",
            applicant_role="Pemilik", document_url=evidence_for(user)["document_url"],
            store_photo_url=evidence_for(user)["store_photo_url"], status="pending", latitude=3.5, longitude=98.6, **extra,
        )
        self.db.add(sub)
        self.db.commit()
        return sub

    def _verified_owner(self, user, store):
        self.db.add(StoreOwner(user_id=user, store_id=store.id, status="verified", verified_at=datetime.now(timezone.utc)))
        self.db.commit()

    def _verify(self, sub):
        self.login(ADMIN, admin=True)
        return self.client.post("/ads/verify-new-store", json={"submission_id": str(sub.id)})

    def test_does_not_overwrite_the_submitters_existing_verified_store(self):
        self._verified_owner(USER, self.store_a)
        sub = self._submission(USER)
        res = self._verify(sub)
        self.assertEqual(res.status_code, 409)
        self.db.expire_all()
        rows = self.owners()
        self.assertEqual([(str(r.store_id), r.status) for r in rows], [(str(self.store_a.id), "verified")])
        self.assertEqual(self.db.query(Store).count(), 2)
        self.assertEqual(self.db.query(StoreSubmission).one().status, "pending")

    def test_does_not_hand_over_a_store_that_already_has_a_verified_owner(self):
        self._verified_owner(OTHER, self.store_a)
        sub = self._submission(USER, name=self.store_a.nama, address=self.store_a.alamat)
        res = self._verify(sub)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.owners(USER), [])
        self.assertEqual(self.owners(OTHER)[0].status, "verified")

    def test_adds_a_new_verified_row_and_keeps_earlier_claim_history(self):
        self.claim(store=self.store_a)
        self.login(ADMIN, admin=True)
        self.client.post("/ads/reject-claim", json={"user_id": USER, "store_id": str(self.store_a.id), "reason": "ditolak"})
        sub = self._submission(USER)
        res = self._verify(sub)
        self.assertEqual(res.status_code, 200)

        rows = self.owners()
        self.assertEqual(sorted(r.status for r in rows), ["rejected", "verified"])
        rejected = [r for r in rows if r.status == "rejected"][0]
        self.assertEqual(rejected.rejection_reason, "ditolak")
        verified = [r for r in rows if r.status == "verified"][0]
        self.assertEqual(str(verified.store_id), res.json()["store_id"])
        self.assertEqual(verified.reviewed_by, ADMIN)
        self.assertEqual(verified.document_url, sub.document_url)

    def test_auto_rejection_does_not_cross_addresses(self):
        approved = self._submission(USER, name="Apotek Sehat", address="Jl. Merdeka No. 1, Jakarta")
        other_city = self._submission(OTHER, name="Apotek Sehat", address="Jl. Pahlawan No. 7, Surabaya")
        res = self._verify(approved)
        self.assertEqual(res.status_code, 200)
        self.db.expire_all()
        self.assertEqual(self.db.get(StoreSubmission, other_city.id).status, "pending")

    def test_auto_rejection_still_applies_to_the_same_name_and_address(self):
        approved = self._submission(USER, name="Apotek Sehat", address="Jl. Merdeka No. 1, Jakarta")
        duplicate = self._submission(OTHER, name="apotek sehat", address="JL. MERDEKA NO. 1, JAKARTA")
        self.assertEqual(self._verify(approved).status_code, 200)
        self.db.expire_all()
        row = self.db.get(StoreSubmission, duplicate.id)
        self.assertEqual(row.status, "rejected")
        self.assertEqual(row.reviewed_by, ADMIN)


if __name__ == "__main__":
    unittest.main()
