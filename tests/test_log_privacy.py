"""Log server tidak boleh memuat id pengguna, nama berkas unggahan, atau nilai parameter SQL (data pemohon)."""
import logging
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from app.database import get_db
from app.dependencies import get_current_user
from app.main import app
from app.models.schemas import ScanResponse
from app.routers.scan import SimpleRateLimiter
from app.services.errors import safe_error

USER = "9e1f6c1a-5b7d-4c0e-8a52-3d4f6a7b8c9d"
FILENAME = "foto_rak_andi_pribadi.jpg"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
SECRET = "Budi Rahasia 081234567890"


def _db_error():
    return DBAPIError("INSERT INTO store_owners (applicant_name) VALUES (%(n)s)", {"n": SECRET}, Exception("orig"))


class TestScanLogPrivacy(unittest.TestCase):
    def setUp(self):
        app.dependency_overrides[get_current_user] = lambda: USER
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def _post(self):
        return self.client.post("/scan/", files={"file": (FILENAME, JPEG, "image/jpeg")})

    def _assert_clean(self, caplog_text):
        self.assertNotIn(USER, caplog_text)
        self.assertNotIn(FILENAME, caplog_text)
        self.assertNotIn("andi", caplog_text)

    def test_success_log_has_metrics_but_no_user_or_filename(self):
        with self.assertLogs("rakoon_backend.scan", level=logging.INFO) as cm, \
                patch("app.routers.scan.process_shelf_image", new=AsyncMock(return_value=ScanResponse(detected=[]))):
            self.assertEqual(self._post().status_code, 200)
        text = "\n".join(cm.output)
        self._assert_clean(text)
        self.assertIn(f"bytes={len(JPEG)}", text)
        self.assertIn("items=0", text)
        self.assertIn("durasi_ms=", text)

    def test_http_failure_log_is_clean(self):
        boom = AsyncMock(side_effect=HTTPException(status_code=502, detail="vision gagal"))
        with self.assertLogs("rakoon_backend.scan", level=logging.INFO) as cm, \
                patch("app.routers.scan.process_shelf_image", new=boom):
            self.assertEqual(self._post().status_code, 502)
        text = "\n".join(cm.output)
        self._assert_clean(text)
        self.assertIn("status=502", text)

    def test_unexpected_failure_log_is_clean(self):
        boom = AsyncMock(side_effect=RuntimeError(f"gagal memproses {FILENAME} milik {USER}"))
        with self.assertLogs("rakoon_backend.scan", level=logging.INFO) as cm, \
                patch("app.routers.scan.process_shelf_image", new=boom):
            client = TestClient(app, raise_server_exceptions=False)
            client.post("/scan/", files={"file": (FILENAME, JPEG, "image/jpeg")})
        text = "\n".join(cm.output)
        self._assert_clean(text)
        self.assertIn("RuntimeError", text)

    def test_rate_limit_log_has_no_user_id(self):
        limiter = SimpleRateLimiter(max_requests=1, window_seconds=60)
        limiter.check(USER)
        with self.assertLogs("rakoon_backend.scan", level=logging.WARNING) as cm:
            with self.assertRaises(HTTPException):
                limiter.check(USER)
        self.assertNotIn(USER, "\n".join(cm.output))


class TestDatabaseErrorLogPrivacy(unittest.TestCase):
    def test_safe_error_does_not_log_sql_parameters(self):
        logger = logging.getLogger("test_log_privacy.safe_error")
        with self.assertLogs(logger, level=logging.ERROR) as cm:
            safe_error(logger, _db_error(), "pesan publik", log_prefix="Simpan klaim")
        text = "\n".join(cm.output)
        self.assertNotIn(SECRET, text)
        self.assertIn("DBAPIError", text)

    def test_claim_store_commit_failure_does_not_log_applicant_data(self):
        app.dependency_overrides[get_current_user] = lambda: USER
        db = MagicMock()
        db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = MagicMock(id="s1")
        db.query.return_value.filter.return_value.first.return_value = None
        db.commit.side_effect = _db_error()
        app.dependency_overrides[get_db] = lambda: db
        try:
            with self.assertLogs("rakoon_backend.ads", level=logging.ERROR) as cm, \
                    patch("app.routers.ads._require_own_evidence"), \
                    patch("app.routers.ads._claim_rows", return_value=[]):
                res = TestClient(app).post("/ads/claim-store", json={
                    "store_id": "00000000-0000-0000-0000-000000000001",
                    "applicant_name": SECRET, "applicant_phone": "081234567890",
                    "document_url": "documents/x", "store_photo_url": "photos/x",
                })
            self.assertEqual(res.status_code, 500)
            self.assertNotIn(SECRET, "\n".join(cm.output))
        finally:
            app.dependency_overrides.clear()


if __name__ == "__main__":
    unittest.main()
