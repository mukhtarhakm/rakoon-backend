import json
import unittest
from unittest.mock import patch, AsyncMock
import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.models.schemas import ScanResultItem, ProductCategory
from app.services.vision_service import (
    process_shelf_image,
    check_needs_escalation,
    clean_numeric,
    clean_str,
    normalize_and_validate_category,
    extract_and_parse_json,
    DEFAULT_PRIMARY_MODEL,
    DEFAULT_VERIFICATION_MODEL,
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

    async def test_first_pass_luna_high_confidence_skips_terra(self):
        """
        Jika model first-pass (gpt-5.6-luna) menghasilkan deteksi lengkap dengan confidence tinggi,
        model verifikasi (gpt-5.6-terra) TIDAK BOLEH dipanggil untuk menghemat biaya (90%+ cost saving).
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
            "PRIMARY_VISION_MODEL": "gpt-5.6-luna",
            "VERIFICATION_VISION_MODEL": "gpt-5.6-terra"
        }):
            response = await process_shelf_image(
                image_bytes=self.dummy_image_bytes,
                content_type=self.content_type,
                http_client=mock_client
            )

        # Hanya 1 pemanggilan API (Luna)
        self.assertEqual(mock_client.post.call_count, 1)
        call_args = mock_client.post.call_args[1]
        self.assertEqual(call_args["json"]["model"], "gpt-5.6-luna")

        # Response harus menggunakan Luna dan tidak eskalasi ke Terra
        self.assertEqual(response.model_used, "gpt-5.6-luna")
        self.assertFalse(response.escalated_to_verification)
        self.assertEqual(len(response.detected), 2)
        self.assertEqual(response.detected[0].nama_produk, "Indomie Goreng 85g")
        self.assertEqual(response.detected[0].confidence, "tinggi")
        self.assertFalse(response.detected[0].needs_verification)

    async def test_escalation_to_terra_on_low_confidence(self):
        """
        Jika hasil Luna memiliki indikasi ketidakpastian (confidence 'rendah'),
        sistem harus mengeskalasi ke gpt-5.6-terra untuk verifikasi penalaran tinggi.
        """
        luna_items = [
            {
                "nama_produk": "Indomie Mi Goreng Spesial",
                "harga": 3500,
                "ukuran": 85,
                "satuan": "g",
                "kategori": "Makanan Instan",
                "confidence": "rendah"  # Indikasi ketidakpastian!
            }
        ]

        terra_verified_items = [
            {
                "nama_produk": "Indomie Mi Goreng Spesial Plus Bawang",
                "harga": 3600,
                "ukuran": 85,
                "satuan": "g",
                "kategori": "Makanan Instan",
                "confidence": "tinggi"  # Berhasil diverifikasi dengan confidence tinggi
            }
        ]

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.side_effect = [
            self._create_mock_response(DEFAULT_PRIMARY_MODEL, luna_items),
            self._create_mock_response(DEFAULT_VERIFICATION_MODEL, terra_verified_items)
        ]

        with patch.dict("os.environ", {
            "AI_VISION_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test-key",
            "PRIMARY_VISION_MODEL": "gpt-5.6-luna",
            "VERIFICATION_VISION_MODEL": "gpt-5.6-terra"
        }):
            response = await process_shelf_image(
                image_bytes=self.dummy_image_bytes,
                content_type=self.content_type,
                http_client=mock_client
            )

        # Harus ada 2 pemanggilan API: 1 Luna, 1 Terra
        self.assertEqual(mock_client.post.call_count, 2)
        first_call = mock_client.post.call_args_list[0][1]
        second_call = mock_client.post.call_args_list[1][1]
        self.assertEqual(first_call["json"]["model"], "gpt-5.6-luna")
        self.assertEqual(second_call["json"]["model"], "gpt-5.6-terra")

        # Response harus mencatat Terra sebagai model verifikasi
        self.assertEqual(response.model_used, "gpt-5.6-terra")
        self.assertTrue(response.escalated_to_verification)
        self.assertEqual(len(response.detected), 1)
        self.assertEqual(response.detected[0].nama_produk, "Indomie Mi Goreng Spesial Plus Bawang")
        self.assertEqual(response.detected[0].harga, 3600)
        self.assertEqual(response.detected[0].confidence, "tinggi")

    async def test_escalation_to_terra_on_missing_critical_info(self):
        """
        Jika Luna mendeteksi item tapi harga atau nama produk null/tidak terbaca jelas,
        sistem harus mengeskalasi ke gpt-5.6-terra.
        """
        luna_items = [
            {
                "nama_produk": "Teh Botol Sosro",
                "harga": None,  # Harga tidak terbaca jelas oleh Luna
                "ukuran": 450,
                "satuan": "ml",
                "kategori": "Minuman",
                "confidence": "tinggi"
            }
        ]

        terra_verified_items = [
            {
                "nama_produk": "Teh Botol Sosro Kotak 450ml",
                "harga": 6500,  # Berhasil dibaca oleh kemampuan penalaran Terra
                "ukuran": 450,
                "satuan": "ml",
                "kategori": "Minuman",
                "confidence": "tinggi"
            }
        ]

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.side_effect = [
            self._create_mock_response(DEFAULT_PRIMARY_MODEL, luna_items),
            self._create_mock_response(DEFAULT_VERIFICATION_MODEL, terra_verified_items)
        ]

        with patch.dict("os.environ", {
            "AI_VISION_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test-key",
            "PRIMARY_VISION_MODEL": "gpt-5.6-luna",
            "VERIFICATION_VISION_MODEL": "gpt-5.6-terra"
        }):
            response = await process_shelf_image(
                image_bytes=self.dummy_image_bytes,
                content_type=self.content_type,
                http_client=mock_client
            )

        self.assertEqual(mock_client.post.call_count, 2)
        self.assertEqual(response.model_used, "gpt-5.6-terra")
        self.assertTrue(response.escalated_to_verification)
        self.assertEqual(response.detected[0].harga, 6500)

    async def test_terra_fallback_resilience_on_failure(self):
        """
        Jika pemanggilan gpt-5.6-terra gagal (misal timeout atau rate limit),
        sistem harus secara anggun (gracefully) fallback ke hasil Luna dengan penanda
        needs_verification=True, bukan menyebabkan crash.
        """
        luna_items = [
            {
                "nama_produk": "Aqua Botol 600ml",
                "harga": 3000,
                "ukuran": 600,
                "satuan": "ml",
                "kategori": "Minuman",
                "confidence": "rendah"  # Menuntut eskalasi
            }
        ]

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        # Luna berhasil, tapi Terra melempar exception
        mock_client.post.side_effect = [
            self._create_mock_response(DEFAULT_PRIMARY_MODEL, luna_items),
            httpx.ConnectTimeout("Terra connection timed out")
        ]

        with patch.dict("os.environ", {
            "AI_VISION_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test-key",
            "PRIMARY_VISION_MODEL": "gpt-5.6-luna",
            "VERIFICATION_VISION_MODEL": "gpt-5.6-terra"
        }):
            response = await process_shelf_image(
                image_bytes=self.dummy_image_bytes,
                content_type=self.content_type,
                http_client=mock_client
            )

        self.assertEqual(mock_client.post.call_count, 2)
        # Fallback ke Luna dengan flag verification
        self.assertEqual(response.model_used, "gpt-5.6-luna")
        self.assertTrue(response.escalated_to_verification)
        self.assertEqual(len(response.detected), 1)
        self.assertTrue(response.detected[0].needs_verification)
        self.assertIn("Verifikasi sekunder terhambat", response.message)

    async def test_groq_fallback_mode(self):
        """
        Jika AI_VISION_PROVIDER diset ke 'groq', sistem harus menggunakan model Groq
        (qwen/qwen3.8-27b) untuk memproses gambar secara gratis tanpa OpenAI.
        """
        groq_items = [
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
        mock_client.post.return_value = self._create_mock_response("qwen/qwen3.8-27b", groq_items)

        with patch.dict("os.environ", {
            "AI_VISION_PROVIDER": "groq",
            "GROQ_API_KEY": "gsk-test-key",
            "GROQ_MODEL": "qwen/qwen3.8-27b"
        }):
            response = await process_shelf_image(
                image_bytes=self.dummy_image_bytes,
                content_type=self.content_type,
                http_client=mock_client
            )

        self.assertEqual(mock_client.post.call_count, 1)
        call_args = mock_client.post.call_args[1]
        self.assertEqual(call_args["json"]["model"], "qwen/qwen3.8-27b")
        self.assertEqual(call_args["json"]["reasoning_effort"], "none")
        self.assertIn("qwen/qwen3.8-27b", response.model_used)
        self.assertEqual(len(response.detected), 1)
        self.assertEqual(response.detected[0].nama_produk, "Sari Roti Tawar Kupas")


class TestVisionHelpers(unittest.TestCase):

    def test_check_needs_escalation_rules(self):
        # 1. High confidence & complete data -> No escalation
        items_good = [
            ScanResultItem(
                nama_produk="Kecap Manis Bango 520ml",
                harga=25000,
                ukuran=520,
                satuan="ml",
                kategori=ProductCategory.BUMBU_SAUS,
                confidence="tinggi",
                needs_verification=False
            )
        ]
        needs_esc, reason = check_needs_escalation(items_good)
        self.assertFalse(needs_esc)
        self.assertEqual(reason, "")

        # 2. Low confidence -> Needs escalation
        items_low_conf = [
            ScanResultItem(
                nama_produk="Kecap Manis Bango",
                harga=25000,
                ukuran=520,
                satuan="ml",
                kategori=ProductCategory.BUMBU_SAUS,
                confidence="rendah",
                needs_verification=True
            )
        ]
        needs_esc, reason = check_needs_escalation(items_low_conf)
        self.assertTrue(needs_esc)
        self.assertIn("confidence rendah", reason)

        # 3. Missing price -> Needs escalation
        items_missing_price = [
            ScanResultItem(
                nama_produk="Minyak Goreng Bimoli 2L",
                harga=None,
                ukuran=2,
                satuan="L",
                kategori=ProductCategory.MAKANAN_POKOK,
                confidence="tinggi",
                needs_verification=True
            )
        ]
        needs_esc, reason = check_needs_escalation(items_missing_price)
        self.assertTrue(needs_esc)
        self.assertIn("Harga produk", reason)

        # 4. Missing name -> Needs escalation
        items_missing_name = [
            ScanResultItem(
                nama_produk=None,
                harga=10000,
                ukuran=100,
                satuan="g",
                kategori=ProductCategory.CAMILAN,
                confidence="tinggi",
                needs_verification=True
            )
        ]
        needs_esc, reason = check_needs_escalation(items_missing_name)
        self.assertTrue(needs_esc)
        self.assertIn("Nama produk", reason)

        # 5. Empty items -> Needs escalation
        needs_esc, reason = check_needs_escalation([])
        self.assertTrue(needs_esc)
        self.assertIn("Tidak ada produk", reason)

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


class TestScanEndpoint(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(app)

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


if __name__ == "__main__":
    unittest.main()
