import os
import sys
import unittest
from datetime import datetime, timezone
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

    def test_banner_owner_query_runs_off_event_loop_thread(self):
        import threading
        from sqlalchemy.orm import Session
        from app.routers import ads

        self._make_verified_owner()
        loop_threads, db_threads = set(), set()
        real_upload, real_query = ads.upload_image, Session.query

        async def upload(*args, **kwargs):
            loop_threads.add(threading.get_ident())
            return await real_upload(*args, **kwargs)

        def query(session, *args, **kwargs):
            db_threads.add(threading.get_ident())
            return real_query(session, *args, **kwargs)

        with patch.object(ads, "upload_image", upload), patch.object(Session, "query", query):
            res = self._post("/ads/upload-banner", PNG, "banner.png")

        self.assertEqual(res.status_code, 200)
        self.assertTrue(loop_threads and db_threads)
        self.assertTrue(db_threads.isdisjoint(loop_threads))


if __name__ == "__main__":
    unittest.main()
