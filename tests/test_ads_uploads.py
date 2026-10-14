import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.dependencies import get_current_user
from app.models.db_models import Store, StoreOwner

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


USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake png body"
PDF = b"%PDF-1.4 fake pdf body"
BIG_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024)


class TestAdsUploads(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: USER_ID
        self.client = TestClient(app)

        self.sb = MagicMock()
        self.bucket = MagicMock()
        self.bucket.get_public_url.side_effect = (
            lambda name: f"https://mock.supabase.co/storage/v1/object/public/product-photos/{name}?"
        )
        self.sb.storage.from_.return_value = self.bucket
        sb_patch = patch("app.services.storage_upload.supabase", self.sb)
        sb_patch.start()
        self.addCleanup(sb_patch.stop)

        self.docs_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.docs_dir.cleanup)
        docs_patch = patch("app.routers.ads.PRIVATE_DOCS_DIR", Path(self.docs_dir.name))
        docs_patch.start()
        self.addCleanup(docs_patch.stop)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _make_verified_owner(self):
        store = Store(nama="Toko Uji", lat=-7.75, lng=110.4)
        self.db.add(store)
        self.db.commit()
        self.db.add(StoreOwner(
            user_id=USER_ID,
            store_id=store.id,
            status="verified",
            verified_at=datetime.now(timezone.utc),
        ))
        self.db.commit()

    def _post(self, path, content, name="f.png", ctype="image/png"):
        return self.client.post(path, files={"file": (name, content, ctype)})

    # --- upload-claim-photo ---

    def test_claim_photo_success_returns_storage_url_with_random_name(self):
        res = self._post("/ads/upload-claim-photo", PNG)
        self.assertEqual(res.status_code, 200)
        url = res.json()["photo_url"]
        self.assertTrue(url.startswith("https://mock.supabase.co/storage/"))
        name = self.bucket.upload.call_args[0][0]
        self.assertRegex(name, r"^claim_photo_[0-9a-f]{32}\.png$")
        self.assertNotIn(USER_ID, name)

    def test_claim_photo_storage_failure_returns_503_not_unsplash(self):
        self.bucket.upload.side_effect = Exception("storage down")
        res = self._post("/ads/upload-claim-photo", PNG)
        self.assertEqual(res.status_code, 503)
        self.assertNotIn("unsplash", res.text.lower())

    def test_claim_photo_with_mock_client_returns_503(self):
        from app.database import MockSupabaseClient
        with patch("app.services.storage_upload.supabase", MockSupabaseClient()):
            res = self._post("/ads/upload-claim-photo", PNG)
        self.assertEqual(res.status_code, 503)

    def test_claim_photo_fake_png_returns_400(self):
        res = self._post("/ads/upload-claim-photo", b"MZ\x90\x00 not an image", "evil.png", "image/png")
        self.assertEqual(res.status_code, 400)
        self.bucket.upload.assert_not_called()

    def test_claim_photo_over_5mb_returns_413(self):
        res = self._post("/ads/upload-claim-photo", BIG_PNG)
        self.assertEqual(res.status_code, 413)

    # --- upload-banner ---

    def test_banner_success_for_verified_owner(self):
        self._make_verified_owner()
        res = self._post("/ads/upload-banner", PNG, "banner.png")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["banner_url"].startswith("https://mock.supabase.co/storage/"))
        self.assertRegex(self.bucket.upload.call_args[0][0], r"^ad_[0-9a-f]{32}\.png$")

    def test_banner_storage_failure_returns_503_not_unsplash(self):
        self._make_verified_owner()
        self.bucket.upload.side_effect = Exception("storage down")
        res = self._post("/ads/upload-banner", PNG, "banner.png")
        self.assertEqual(res.status_code, 503)
        self.assertNotIn("unsplash", res.text.lower())

    def test_banner_fake_png_returns_400_and_oversize_413(self):
        self._make_verified_owner()
        self.assertEqual(self._post("/ads/upload-banner", b"<html>x</html>", "b.png").status_code, 400)
        self.assertEqual(self._post("/ads/upload-banner", BIG_PNG, "b.png").status_code, 413)

    def test_banner_non_owner_still_forbidden(self):
        self.assertEqual(self._post("/ads/upload-banner", PNG, "banner.png").status_code, 403)

    # --- upload-claim-document (lokasi simpan tetap private_storage) ---

    def test_claim_document_pdf_saved_locally_with_user_prefixed_name(self):
        res = self._post("/ads/upload-claim-document", PDF, "nib.pdf", "application/pdf")
        self.assertEqual(res.status_code, 200)
        filename = res.json()["filename"]
        self.assertRegex(filename, rf"^doc_{USER_ID}_[0-9a-f]{{16}}\.pdf$")
        self.assertEqual((Path(self.docs_dir.name) / filename).read_bytes(), PDF)
        self.bucket.upload.assert_not_called()

    def test_claim_document_fake_pdf_by_extension_and_content_type_returns_400(self):
        res = self._post("/ads/upload-claim-document", b"#!/bin/sh\necho hi", "nib.pdf", "application/pdf")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(list(Path(self.docs_dir.name).iterdir()), [])

    def test_claim_document_over_5mb_returns_413(self):
        res = self._post("/ads/upload-claim-document", b"%PDF" + b"\x00" * (5 * 1024 * 1024), "nib.pdf", "application/pdf")
        self.assertEqual(res.status_code, 413)

    def test_claim_document_png_scan_accepted(self):
        res = self._post("/ads/upload-claim-document", PNG, "scan.png")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["filename"].endswith(".png"))


if __name__ == "__main__":
    unittest.main()
