"""Claim evidence (document + store photo) lives in the private Supabase bucket `claim-document`.
Supabase is always mocked."""
import os
import sys
import uuid
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db, MockSupabaseClient
from app.dependencies import get_current_user, get_current_admin_user, get_current_user_profile
from app.models.db_models import Store, StoreOwner, StoreSubmission
from app.services import storage_upload

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


USER_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ID = "22222222-2222-2222-2222-222222222222"
PDF = b"%PDF-1.4 fake pdf body"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"fake jpeg body"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake png body"
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"fake webp body"
DOC_PATH_RE = r"^documents/[0-9a-f]{48}\.(pdf|jpg)$"
PHOTO_PATH_RE = r"^photos/[0-9a-f]{48}\.jpg$"


class ClaimEvidenceBase(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: USER_ID
        self.client = TestClient(app)

        self.sb = MagicMock()
        self.bucket = MagicMock()
        self.bucket.create_signed_url.return_value = {"signedURL": "https://mock.supabase.co/storage/v1/object/sign/claim-document/x?token=t"}
        self.sb.storage.from_.return_value = self.bucket
        for patcher in (
            patch("app.services.storage_upload.supabase", self.sb),
            patch.dict(os.environ, {"SUPABASE_SERVICE_KEY": "unit-test-service-key"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _post(self, route, content, name="f.bin", ctype="application/octet-stream"):
        return self.client.post(route, files={"file": (name, content, ctype)})

    def _as_admin(self):
        app.dependency_overrides[get_current_admin_user] = lambda: {"user_id": OTHER_ID, "role": "admin"}

    def _as_plain_user(self):
        app.dependency_overrides.pop(get_current_admin_user, None)
        app.dependency_overrides[get_current_user_profile] = lambda: {"id": USER_ID, "role": "user", "email": "u@example.com"}


class TestUploadToPrivateBucket(ClaimEvidenceBase):
    def test_pdf_document_goes_to_the_private_bucket_with_a_random_path_without_user_id(self):
        res = self._post("/ads/upload-claim-document", PDF, "nib.pdf", "application/pdf")
        self.assertEqual(res.status_code, 200)
        self.sb.storage.from_.assert_called_with("claim-document")
        path, body, options = self.bucket.upload.call_args[0]
        self.assertRegex(path, DOC_PATH_RE)
        self.assertNotIn(USER_ID, path)
        self.assertEqual(body, PDF)
        self.assertEqual(options["content-type"], "application/pdf")
        self.assertEqual(options["upsert"], "false")
        self.assertEqual(res.json()["document_path"], path)
        self.bucket.get_public_url.assert_not_called()

    def test_jpeg_document_is_accepted(self):
        res = self._post("/ads/upload-claim-document", JPEG, "scan.jpg", "image/jpeg")
        self.assertEqual(res.status_code, 200)
        self.assertRegex(res.json()["document_path"], DOC_PATH_RE)

    def test_document_rejects_png_webp_and_fake_pdf(self):
        for content, name, ctype in (
            (PNG, "a.png", "image/png"),
            (WEBP, "a.webp", "image/webp"),
            (b"#!/bin/sh\necho hi", "nib.pdf", "application/pdf"),
        ):
            res = self._post("/ads/upload-claim-document", content, name, ctype)
            self.assertEqual(res.status_code, 400, name)
        self.bucket.upload.assert_not_called()

    def test_document_over_5mb_returns_413(self):
        res = self._post("/ads/upload-claim-document", b"%PDF" + b"\x00" * (5 * 1024 * 1024), "nib.pdf", "application/pdf")
        self.assertEqual(res.status_code, 413)
        self.bucket.upload.assert_not_called()

    def test_photo_accepts_jpeg_only(self):
        ok = self._post("/ads/upload-claim-photo", JPEG, "front.jpg", "image/jpeg")
        self.assertEqual(ok.status_code, 200)
        self.assertRegex(ok.json()["photo_path"], PHOTO_PATH_RE)
        self.sb.storage.from_.assert_called_with("claim-document")

        self.bucket.upload.reset_mock()
        for content, name, ctype in ((PNG, "a.png", "image/png"), (PDF, "a.pdf", "application/pdf"), (WEBP, "a.webp", "image/webp")):
            self.assertEqual(self._post("/ads/upload-claim-photo", content, name, ctype).status_code, 400, name)
        self.bucket.upload.assert_not_called()

    def test_each_upload_gets_a_different_path(self):
        a = self._post("/ads/upload-claim-document", PDF, "a.pdf").json()["document_path"]
        b = self._post("/ads/upload-claim-document", PDF, "a.pdf").json()["document_path"]
        self.assertNotEqual(a, b)

    def test_storage_failure_returns_503_without_any_path(self):
        self.bucket.upload.side_effect = Exception("storage down")
        for route, content in (("/ads/upload-claim-document", PDF), ("/ads/upload-claim-photo", JPEG)):
            res = self._post(route, content)
            self.assertEqual(res.status_code, 503, route)
            self.assertNotIn("path", res.text.lower().replace("storage down", ""))
            self.assertNotIn("storage down", res.text)

    def test_mock_supabase_client_returns_503(self):
        with patch("app.services.storage_upload.supabase", MockSupabaseClient()):
            self.assertEqual(self._post("/ads/upload-claim-document", PDF).status_code, 503)
            self.assertEqual(self._post("/ads/upload-claim-photo", JPEG).status_code, 503)

    def test_missing_service_key_fails_closed(self):
        with patch.dict(os.environ, {"SUPABASE_SERVICE_KEY": ""}):
            self.assertEqual(self._post("/ads/upload-claim-document", PDF).status_code, 503)
        self.bucket.upload.assert_not_called()

    def test_path_ownership_helper_binds_a_path_to_its_uploader(self):
        path = self._post("/ads/upload-claim-document", PDF).json()["document_path"]
        self.assertTrue(storage_upload.claim_path_belongs_to(path, USER_ID, "documents"))
        self.assertFalse(storage_upload.claim_path_belongs_to(path, OTHER_ID, "documents"))
        self.assertFalse(storage_upload.claim_path_belongs_to(path, USER_ID, "photos"))
        self.assertFalse(storage_upload.claim_path_belongs_to("documents/" + "0" * 48 + ".pdf", USER_ID, "documents"))
        self.assertFalse(storage_upload.claim_path_belongs_to("../" + path, USER_ID, "documents"))


class TestLegacyDocumentEndpoint(ClaimEvidenceBase):
    def test_requires_login_before_anything_else(self):
        app.dependency_overrides.pop(get_current_user, None)
        res = self.client.get("/ads/documents/doc_anything.pdf")
        self.assertIn(res.status_code, (401, 403))

    def test_is_disabled_and_does_not_reveal_whether_a_file_exists(self):
        self._as_plain_user()
        for name in ("doc_missing.pdf", f"doc_{USER_ID}_0123456789abcdef.pdf"):
            res = self.client.get(f"/ads/documents/{name}")
            self.assertEqual(res.status_code, 410, name)


class TestAdminSignedUrl(ClaimEvidenceBase):
    def _referenced_path(self):
        path = self._post("/ads/upload-claim-document", PDF).json()["document_path"]
        store = Store(nama="Toko Bukti", lat=-7.7, lng=110.4)
        self.db.add(store)
        self.db.commit()
        self.db.add(StoreOwner(user_id=USER_ID, store_id=store.id, status="pending", document_url=path))
        self.db.commit()
        return path

    def test_admin_gets_a_short_lived_signed_url_for_referenced_evidence(self):
        path = self._referenced_path()
        self._as_admin()
        res = self.client.get("/ads/claim-evidence-url", params={"path": path})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["url"].startswith("https://mock.supabase.co/"))
        used_path, ttl = self.bucket.create_signed_url.call_args[0][:2]
        self.assertEqual(used_path, path)
        self.assertLessEqual(ttl, 900)
        self.assertEqual(res.json()["expires_in"], ttl)
        self.assertIn("no-store", res.headers.get("cache-control", ""))

    def test_photo_path_on_a_submission_is_also_signable(self):
        photo = self._post("/ads/upload-claim-photo", JPEG).json()["photo_path"]
        self.db.add(StoreSubmission(
            user_id=USER_ID, store_name="Warung", store_address="Jl. Contoh 1", applicant_name="A",
            applicant_phone="08123456789", applicant_role="Pemilik", store_photo_url=photo, latitude=1.0, longitude=2.0,
        ))
        self.db.commit()
        self._as_admin()
        self.assertEqual(self.client.get("/ads/claim-evidence-url", params={"path": photo}).status_code, 200)

    def test_non_admin_gets_403_even_for_their_own_evidence(self):
        path = self._referenced_path()
        self._as_plain_user()
        res = self.client.get("/ads/claim-evidence-url", params={"path": path})
        self.assertEqual(res.status_code, 403)
        self.bucket.create_signed_url.assert_not_called()

    def test_unreferenced_or_malformed_paths_are_refused(self):
        self._as_admin()
        unknown = "documents/" + uuid.uuid4().hex + "0" * 16 + ".pdf"
        self.assertEqual(self.client.get("/ads/claim-evidence-url", params={"path": unknown}).status_code, 404)
        for bad in ("../secrets.pdf", "documents/../x.pdf", "documents/abc.pdf", "product-photos/" + uuid.uuid4().hex + "0" * 16 + ".jpg", ""):
            self.assertEqual(self.client.get("/ads/claim-evidence-url", params={"path": bad}).status_code, 400, bad)
        self.bucket.create_signed_url.assert_not_called()

    def test_prefix_of_a_referenced_path_does_not_match(self):
        path = self._referenced_path()
        self._as_admin()
        partial = path[: len(path) - 5]  # not an exact match (and not a valid path shape)
        self.assertEqual(self.client.get("/ads/claim-evidence-url", params={"path": partial}).status_code, 400)

    def test_signing_failure_returns_503(self):
        path = self._referenced_path()
        self._as_admin()
        self.bucket.create_signed_url.side_effect = Exception("boom")
        res = self.client.get("/ads/claim-evidence-url", params={"path": path})
        self.assertEqual(res.status_code, 503)
        self.assertNotIn("boom", res.text)


if __name__ == "__main__":
    unittest.main()
