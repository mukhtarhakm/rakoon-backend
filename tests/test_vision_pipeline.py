import json
import unittest
from unittest.mock import patch, AsyncMock
import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.dependencies import get_current_user
from app.models.schemas import ScanResultItem, ProductCategory, ScanResponse
from app.services.vision_service import (
    process_shelf_image,
    clean_numeric,
    clean_str,
    normalize_and_validate_category,
    extract_and_parse_json,
    DEFAULT_PRIMARY_MODEL,
)


class TestVisionPipeline(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.dummy_image_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00"
        self.content_type = "image/jpeg"

    def _create_mock_response(self, model: str, detected_items: list, status_code: int = 200) -> httpx.Response:
        content = json.dumps({"detected": detected_items})
        payload = {
            "id": "chatcmpl-test",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content
                    },
                    "finish_reason": "stop"
                }
            ]
        }
        return httpx.Response(
            status_code=status_code,
            json=payload,
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        )

    async def test_luna_direct_inference_success(self):
        """
        Model gpt-5.6-luna digunakan langsung untuk memproses gambar rak,
        hanya melakukan 1 kali pemanggilan API (tanpa model Terra).
        """
        luna_items = [
            {
                "nama_produk": "Indomie Goreng 85g",
                "harga": 3500,
                "ukuran": 85,
                "satuan": "g",
                "kategori": "Makanan Instan",
                "confidence": "tinggi"
            },
            {
                "nama_produk": "Ultra Milk Cokelat 250ml",
                "harga": 6500,
                "ukuran": 250,
                "satuan": "ml",
                "kategori": "Susu & Olahan",
                "confidence": "tinggi"
            }
        ]

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = self._create_mock_response(DEFAULT_PRIMARY_MODEL, luna_items)

        with patch.dict("os.environ", {
            "AI_VISION_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test-key",
            "PRIMARY_VISION_MODEL": "gpt-5.6-luna"
        }):
            response = await process_shelf_image(
                image_bytes=self.dummy_image_bytes,
                content_type=self.content_type,
                http_client=mock_client
            )

        # Hanya 1 kali pemanggilan API ke model Luna
        self.assertEqual(mock_client.post.call_count, 1)
        call_args = mock_client.post.call_args[1]
        self.assertEqual(call_args["json"]["model"], "gpt-5.6-luna")

        # Response harus menggunakan Luna dan tidak eskalasi
        self.assertEqual(response.model_used, "gpt-5.6-luna")
        self.assertFalse(response.escalated_to_verification)
        self.assertEqual(len(response.detected), 2)
        self.assertEqual(response.detected[0].nama_produk, "Indomie Goreng 85g")
        self.assertEqual(response.detected[0].confidence, "tinggi")
        self.assertFalse(response.detected[0].needs_verification)

    async def test_luna_handles_low_confidence_without_terra(self):
        """
        Jika hasil Luna memiliki confidence 'rendah', item langsung dikembalikan
        dengan needs_verification=True tanpa memanggil Terra.
        """
        luna_items = [
            {
                "nama_produk": "Indomie Mi Goreng Spesial",
                "harga": 3500,
                "ukuran": 85,
                "satuan": "g",
                "kategori": "Makanan Instan",
                "confidence": "rendah"
            }
        ]

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = self._create_mock_response(DEFAULT_PRIMARY_MODEL, luna_items)

        with patch.dict("os.environ", {
            "AI_VISION_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test-key",
            "PRIMARY_VISION_MODEL": "gpt-5.6-luna"
        }):
            response = await process_shelf_image(
                image_bytes=self.dummy_image_bytes,
                content_type=self.content_type,
                http_client=mock_client
            )

        # Tetap hanya 1 kali pemanggilan (tidak ada eskalasi ke Terra)
        self.assertEqual(mock_client.post.call_count, 1)
        self.assertEqual(response.model_used, "gpt-5.6-luna")
        self.assertFalse(response.escalated_to_verification)
        self.assertEqual(len(response.detected), 1)
        self.assertEqual(response.detected[0].confidence, "rendah")
        self.assertTrue(response.detected[0].needs_verification)

    async def test_luna_handles_missing_critical_info_without_terra(self):
        """
        Jika Luna mendeteksi item tapi harga null, item langsung dikembalikan
        dengan needs_verification=True tanpa memanggil model sekunder.
        """
        luna_items = [
            {
                "nama_produk": "Teh Botol Sosro",
                "harga": None,
                "ukuran": 450,
                "satuan": "ml",
                "kategori": "Minuman",
                "confidence": "tinggi"
            }
        ]

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = self._create_mock_response(DEFAULT_PRIMARY_MODEL, luna_items)

        with patch.dict("os.environ", {
            "AI_VISION_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test-key",
            "PRIMARY_VISION_MODEL": "gpt-5.6-luna"
        }):
            response = await process_shelf_image(
                image_bytes=self.dummy_image_bytes,
                content_type=self.content_type,
                http_client=mock_client
            )

        self.assertEqual(mock_client.post.call_count, 1)
        self.assertEqual(response.model_used, "gpt-5.6-luna")
        self.assertFalse(response.escalated_to_verification)
        self.assertIsNone(response.detected[0].harga)
        self.assertTrue(response.detected[0].needs_verification)

    async def test_luna_failure_resilience_on_api_error(self):
        """
        Jika pemanggilan gpt-5.6-luna gagal (misal koneksi terputus),
        sistem harus mengembalikan respons error dengan anggun tanpa crash.
        """
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.side_effect = httpx.ConnectTimeout("Connection timed out")

        with patch.dict("os.environ", {
            "AI_VISION_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test-key",
            "PRIMARY_VISION_MODEL": "gpt-5.6-luna"
        }):
            response = await process_shelf_image(
                image_bytes=self.dummy_image_bytes,
                content_type=self.content_type,
                http_client=mock_client
            )

        self.assertEqual(mock_client.post.call_count, 1)
        self.assertEqual(response.model_used, "gpt-5.6-luna")
        self.assertFalse(response.escalated_to_verification)
        self.assertEqual(len(response.detected), 0)
        self.assertIn("Gagal memproses gambar", response.message)

    async def test_custom_primary_vision_model(self):
        """
        Memastikan custom PRIMARY_VISION_MODEL dari environment variable digunakan dengan benar.
        """
        custom_items = [
            {
                "nama_produk": "Sari Roti Tawar Kupas",
                "harga": 16000,
                "ukuran": 200,
                "satuan": "g",
                "kategori": "Makanan Pokok",
                "confidence": "tinggi"
            }
        ]

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = self._create_mock_response("gpt-5.6-custom", custom_items)

        with patch.dict("os.environ", {
            "OPENAI_API_KEY": "sk-test-key",
            "PRIMARY_VISION_MODEL": "gpt-5.6-custom"
        }):
            response = await process_shelf_image(
                image_bytes=self.dummy_image_bytes,
                content_type=self.content_type,
                http_client=mock_client
            )

        self.assertEqual(mock_client.post.call_count, 1)
        call_args = mock_client.post.call_args[1]
        self.assertEqual(call_args["json"]["model"], "gpt-5.6-custom")
        self.assertEqual(response.model_used, "gpt-5.6-custom")
        self.assertEqual(len(response.detected), 1)
        self.assertEqual(response.detected[0].nama_produk, "Sari Roti Tawar Kupas")


class TestVisionHelpers(unittest.TestCase):

    def test_clean_helpers(self):
        self.assertEqual(clean_numeric("Rp 25.000"), 25000.0)
        self.assertEqual(clean_numeric(15000), 15000.0)
        self.assertIsNone(clean_numeric("N/A"))
        self.assertEqual(clean_str(" Indomie Goreng "), "Indomie Goreng")
        self.assertIsNone(clean_str("null"))
        self.assertIsNone(clean_str("None"))
        self.assertEqual(normalize_and_validate_category("Makanan Instan"), "Makanan Instan")
        self.assertEqual(normalize_and_validate_category("makanan_pokok"), "Makanan Pokok")
        self.assertEqual(normalize_and_validate_category("Kategori ngawur"), "Lainnya")

    def test_extract_and_parse_json(self):
        raw1 = '{"detected": [{"nama_produk": "Test", "harga": 1000, "confidence": "tinggi"}]}'
        parsed1 = extract_and_parse_json(raw1)
        self.assertEqual(len(parsed1["detected"]), 1)

        raw2 = '```json\n{"detected": []}\n```'
        parsed2 = extract_and_parse_json(raw2)
        self.assertEqual(parsed2["detected"], [])

        raw3 = '<think>Analisis rak produk...</think>{"detected": [{"nama_produk": "A", "harga": 2000, "confidence": "tinggi"}]}'
        parsed3 = extract_and_parse_json(raw3)
        self.assertEqual(parsed3["detected"][0]["nama_produk"], "A")


USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class TestScanEndpoint(unittest.TestCase):

    def setUp(self):
        from app.routers.scan import scan_rate_limiter
        scan_rate_limiter.requests.clear()
        self.current_user = USER_A
        app.dependency_overrides[get_current_user] = lambda: self.current_user
        self.client = TestClient(app)
        self.bad_file = {"file": ("test.txt", b"invalid", "text/plain")}

    def tearDown(self):
        from app.routers.scan import scan_rate_limiter
        scan_rate_limiter.requests.clear()
        app.dependency_overrides.pop(get_current_user, None)

    def _post_bad(self, user=None, headers=None):
        if user:
            self.current_user = user
        return self.client.post("/scan/", files=self.bad_file, headers=headers or {})

    def test_scan_invalid_extension(self):
        response = self.client.post(
            "/scan/",
            files={"file": ("document.pdf", b"%PDF-1.4", "application/pdf")}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("bukan gambar yang didukung", response.json()["detail"])

    def test_scan_empty_file(self):
        response = self.client.post(
            "/scan/",
            files={"file": ("empty.jpg", b"", "image/jpeg")}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("kosong", response.json()["detail"])

    def test_scan_file_too_large(self):
        large_bytes = b"0" * (5 * 1024 * 1024 + 1)
        response = self.client.post(
            "/scan/",
            files={"file": ("large.jpg", large_bytes, "image/jpeg")}
        )
        self.assertEqual(response.status_code, 413)
        self.assertIn("terlalu besar", response.json()["detail"])

    def test_scan_requires_login_and_never_calls_vision_without_token(self):
        app.dependency_overrides.pop(get_current_user, None)
        with patch("app.routers.scan.process_shelf_image", new_callable=AsyncMock) as mock_vision:
            for headers in ({}, {"Authorization": "Bearer not-a-real-token"}):
                response = self.client.post(
                    "/scan/",
                    files={"file": ("shelf.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
                    headers=headers,
                )
                self.assertEqual(response.status_code, 401)
            mock_vision.assert_not_called()

    def test_scan_success_for_logged_in_user_calls_vision_once(self):
        with patch("app.routers.scan.process_shelf_image", new_callable=AsyncMock) as mock_vision:
            mock_vision.return_value = ScanResponse(detected=[], message="ok", model_used="mock")
            response = self.client.post(
                "/scan/",
                files={"file": ("shelf.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
            )
        self.assertEqual(response.status_code, 200)
        mock_vision.assert_awaited_once()

    def test_scan_rate_limit_is_per_user_and_returns_429(self):
        for _ in range(10):
            self.assertEqual(self._post_bad(USER_A).status_code, 400)

        blocked = self._post_bad(USER_A)
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Terlalu banyak permintaan scan", blocked.json()["detail"])
        self.assertIn("Retry-After", blocked.headers)

    def test_one_user_exhausting_quota_does_not_block_another_user(self):
        for _ in range(11):
            self._post_bad(USER_A)
        self.assertEqual(self._post_bad(USER_A).status_code, 429)
        self.assertEqual(self._post_bad(USER_B).status_code, 400)  # B tetap punya jatah penuh

    def test_x_forwarded_for_cannot_bypass_or_share_the_limit(self):
        for i in range(10):
            res = self._post_bad(USER_A, headers={"X-Forwarded-For": f"10.0.0.{i}"})
            self.assertEqual(res.status_code, 400)
        # IP berganti-ganti tidak memberi jatah baru
        self.assertEqual(self._post_bad(USER_A, headers={"X-Forwarded-For": "10.9.9.9"}).status_code, 429)
        # IP yang sama dengan user lain tidak berbagi jatah
        self.assertEqual(self._post_bad(USER_B, headers={"X-Forwarded-For": "10.0.0.1"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
