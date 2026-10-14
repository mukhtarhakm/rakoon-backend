import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, File, UploadFile
from fastapi.testclient import TestClient

from app.services import storage_upload
from app.services.storage_upload import MAX_UPLOAD_BYTES, upload_image

PNG = b"\x89PNG\r\n\x1a\n" + b"fake png body"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"fake jpeg body"
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"fake webp body"
PDF = b"%PDF-1.4 fake pdf body"

test_app = FastAPI()


@test_app.post("/img")
async def _img(file: UploadFile = File(...)):
    return {"url": await upload_image(file, prefix="t_")}


def _mock_storage(public_url="https://mock.supabase.co/storage/v1/object/public/product-photos/"):
    sb = MagicMock()
    bucket = MagicMock()
    bucket.get_public_url.side_effect = lambda name: public_url + name + "?"
    sb.storage.from_.return_value = bucket
    return sb, bucket


class TestStorageUploadHelper(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(test_app)

    def _post(self, content, name="a.png", ctype="image/png"):
        return self.client.post("/img", files={"file": (name, content, ctype)})

    def test_success_returns_url_from_storage_with_random_name(self):
        sb, bucket = _mock_storage()
        with patch.object(storage_upload, "supabase", sb):
            res = self._post(PNG)
        self.assertEqual(res.status_code, 200)
        url = res.json()["url"]
        self.assertTrue(url.startswith("https://mock.supabase.co/storage/"))
        self.assertFalse(url.endswith("?"))
        uploaded_name = bucket.upload.call_args[0][0]
        self.assertRegex(uploaded_name, r"^t_[0-9a-f]{32}\.png$")
        self.assertEqual(bucket.upload.call_args[0][2]["content-type"], "image/png")
        self.assertTrue(url.endswith(uploaded_name))

    def test_each_upload_gets_a_different_name(self):
        sb, bucket = _mock_storage()
        with patch.object(storage_upload, "supabase", sb):
            self._post(PNG)
            self._post(PNG)
        names = [c[0][0] for c in bucket.upload.call_args_list]
        self.assertEqual(len(set(names)), 2)

    def test_jpeg_and_webp_magic_bytes_accepted_regardless_of_extension(self):
        sb, bucket = _mock_storage()
        with patch.object(storage_upload, "supabase", sb):
            self.assertEqual(self._post(JPEG, "x.bin", "application/octet-stream").status_code, 200)
            self.assertEqual(self._post(WEBP, "x.dat", "text/plain").status_code, 200)
        names = [c[0][0] for c in bucket.upload.call_args_list]
        self.assertTrue(names[0].endswith(".jpg"))
        self.assertTrue(names[1].endswith(".webp"))

    def test_storage_failure_returns_503_without_local_fallback(self):
        sb, bucket = _mock_storage()
        bucket.upload.side_effect = Exception("storage down")
        with patch.object(storage_upload, "supabase", sb):
            res = self._post(PNG)
        self.assertEqual(res.status_code, 503)
        self.assertNotIn("unsplash", res.text.lower())
        self.assertNotIn("/static/", res.text)

    def test_client_without_storage_attribute_returns_503(self):
        from app.database import MockSupabaseClient
        with patch.object(storage_upload, "supabase", MockSupabaseClient()):
            res = self._post(PNG)
        self.assertEqual(res.status_code, 503)

    def test_storage_returns_no_public_url_returns_502(self):
        sb, bucket = _mock_storage()
        bucket.get_public_url.side_effect = lambda name: ""
        with patch.object(storage_upload, "supabase", sb):
            res = self._post(PNG)
        self.assertEqual(res.status_code, 502)

    def test_fake_png_with_png_extension_and_content_type_returns_400(self):
        sb, bucket = _mock_storage()
        with patch.object(storage_upload, "supabase", sb):
            res = self._post(b"<script>alert(1)</script>", "evil.png", "image/png")
        self.assertEqual(res.status_code, 400)
        bucket.upload.assert_not_called()

    def test_pdf_is_rejected_as_image(self):
        sb, bucket = _mock_storage()
        with patch.object(storage_upload, "supabase", sb):
            res = self._post(PDF, "doc.png", "image/png")
        self.assertEqual(res.status_code, 400)
        bucket.upload.assert_not_called()

    def test_empty_file_returns_400(self):
        res = self._post(b"")
        self.assertEqual(res.status_code, 400)

    def test_file_over_5mb_returns_413(self):
        sb, bucket = _mock_storage()
        big = PNG + b"\x00" * (MAX_UPLOAD_BYTES - len(PNG) + 1)
        self.assertEqual(len(big), MAX_UPLOAD_BYTES + 1)
        with patch.object(storage_upload, "supabase", sb):
            res = self._post(big)
        self.assertEqual(res.status_code, 413)
        bucket.upload.assert_not_called()

    def test_file_exactly_5mb_is_accepted(self):
        sb, bucket = _mock_storage()
        exact = PNG + b"\x00" * (MAX_UPLOAD_BYTES - len(PNG))
        with patch.object(storage_upload, "supabase", sb):
            res = self._post(exact)
        self.assertEqual(res.status_code, 200)

    def test_detect_content_type_pdf_only_when_allowed(self):
        self.assertEqual(storage_upload.detect_content_type(PDF, allow_pdf=True), ("application/pdf", ".pdf"))
        with self.assertRaises(Exception) as ctx:
            storage_upload.detect_content_type(PDF)
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
