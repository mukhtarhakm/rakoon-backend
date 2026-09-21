import os
import base64
import json
import re
import logging
from typing import Optional, List, Union, Tuple, Any

import httpx
from app.models.schemas import (
    ScanResponse,
    ScanResultItem,
    ProductCategory,
)

logger = logging.getLogger("rakoon_backend.vision")

# Model definitions:
# - Primary: gpt-5.6-luna ($0.20/1M input, $1.20/1M output)
DEFAULT_PRIMARY_MODEL = "gpt-5.6-luna"
DEFAULT_API_BASE_URL = "https://api.openai.com/v1"

LUNA_SYSTEM_PROMPT = (
    "You are a fast, high-volume computer vision assistant specialized in supermarket shelf retail product identification. "
    "Output strictly valid JSON matching the requested schema. Do not include markdown code blocks, "
    "conversational explanations, or reasoning thoughts."
)

LUNA_USER_PROMPT = (
    "Identifikasi produk-produk di rak supermarket yang ada pada foto ini. "
    "Temukan produk sebanyak-banyaknya yang terdeteksi dengan jelas. "
    "Untuk setiap produk, kembalikan data berikut:\n"
    "- nama_produk: nama produk lengkap beserta varian (string, null jika tidak terbaca)\n"
    "- harga: harga produk dalam nominal angka murni tanpa Rp atau titik (number, null jika tidak terbaca)\n"
    "- ukuran: ukuran/volume/berat produk (number, null jika tidak terbaca)\n"
    "- satuan: satuan ukuran seperti ml, gr, g, kg, pcs, dll. (string, null jika tidak terbaca)\n"
    "- kategori: kategori produk yang HARUS dipilih dari daftar authoritative berikut:\n"
    "  * Makanan Pokok\n"
    "  * Makanan Instan\n"
    "  * Camilan\n"
    "  * Minuman\n"
    "  * Susu & Olahan\n"
    "  * Bumbu & Saus\n"
    "  * Perawatan Diri\n"
    "  * Produk Rumah Tangga\n"
    "  * Kesehatan\n"
    "  * Bayi\n"
    "  * Lainnya\n"
    "  AI TIDAK BOLEH membuat kategori baru di luar daftar di atas. Jika tidak yakin atau tidak ada yang cocok, gunakan 'Lainnya'.\n"
    "- confidence: 'tinggi' jika Anda sangat yakin dengan informasinya, 'rendah' jika ragu-ragu atau ada ketidakjelasan.\n\n"
    "Kembalikan hasilnya dalam format JSON dengan kunci utama bernama 'detected':\n"
    "{\"detected\": [{\"nama_produk\": \"Indomie Goreng\", \"harga\": 3500, \"ukuran\": 85, \"satuan\": \"g\", \"kategori\": \"Makanan Instan\", \"confidence\": \"tinggi\"}]}\n"
    "Jika sama sekali tidak ada produk yang terdeteksi di foto, kembalikan 'detected' sebagai array kosong []."
)


def get_api_credentials() -> Tuple[str, str, str, str]:
    """
    Mengambil konfigurasi provider, API key, base URL, serta model utama (gpt-5.6-luna).
    Provider yang didukung:
    - 'groq' : Fallback gratis sementara (menggunakan model Qwen/Llama di Groq LPU)
    - 'openai': Model utama gpt-5.6-luna
    """
    provider = os.getenv("AI_VISION_PROVIDER", "").strip().lower()

    if provider == "groq":
        api_key = (os.getenv("GROQ_API_KEY") or "").strip()
        base_url = (os.getenv("GROQ_BASE_URL") or "https://api.groq.com/openai/v1").rstrip("/")
        model = os.getenv("GROQ_MODEL") or "qwen/qwen3.8-27b"
        return "groq", api_key, base_url, model

    # Default provider: openai
    api_key = (os.getenv("OPENAI_API_KEY") or os.getenv("AI_API_KEY") or "").strip()
    base_url = (os.getenv("OPENAI_BASE_URL") or os.getenv("AI_BASE_URL") or DEFAULT_API_BASE_URL).rstrip("/")
    primary_model = os.getenv("PRIMARY_VISION_MODEL") or os.getenv("OPENAI_MODEL_PRIMARY") or DEFAULT_PRIMARY_MODEL

    return "openai", api_key, base_url, primary_model


def clean_numeric(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        val_str = str(val).strip()
        val_str = val_str.replace("Rp", "").replace("rp", "").replace(" ", "").replace(".", "")
        return float(val_str)
    except ValueError:
        return None


def clean_str(val: Any) -> Optional[str]:
    if val is None:
        return None
    val_str = str(val).strip()
    if not val_str or val_str.lower() in ("null", "none"):
        return None
    return val_str


def normalize_and_validate_category(val: Any) -> str:
    cleaned = clean_str(val)
    if not cleaned:
        return ProductCategory.LAINNYA.value

    # 1. Check direct matches against ProductCategory values (case-insensitive)
    for member in ProductCategory:
        if member.value.lower() == cleaned.lower():
            return member.value

    # 2. Check matches against ProductCategory names (case-insensitive)
    for member in ProductCategory:
        if member.name.lower() == cleaned.replace(" ", "_").replace("&", "").replace("__", "_").lower():
            return member.value

    return ProductCategory.LAINNYA.value


def extract_and_parse_json(text: str) -> dict:
    """
    Ekstrak JSON dari teks mentah yang dihasilkan oleh LLM.
    Mendukung format JSON bersih, JSON di dalam block markdown (```json ... ```),
    serta membersihkan tag reasoning/thinking (<think>...</think>) jika ada.
    """
    cleaned = text.strip()
    cleaned = re.sub(r'<think>.*?</think>', '', cleaned, flags=re.DOTALL).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    markdown_match = re.search(r'```(?:json)?\s*(.*?)\s*```', cleaned, re.DOTALL)
    if markdown_match:
        try:
            return json.loads(markdown_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    first_brace = cleaned.find('{')
    last_brace = cleaned.rfind('}')
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(cleaned[first_brace:last_brace + 1])
        except json.JSONDecodeError:
            pass

    first_bracket = cleaned.find('[')
    last_bracket = cleaned.rfind(']')
    if first_bracket != -1 and last_bracket != -1 and last_bracket > first_bracket:
        try:
            return json.loads(cleaned[first_bracket:last_bracket + 1])
        except json.JSONDecodeError:
            pass

    raise json.JSONDecodeError("Gagal mengekstrak JSON dari respon AI.", cleaned, 0)


def parse_raw_items(raw_items: Any) -> List[ScanResultItem]:
    """
    Mengubah list dictionary mentah dari respons AI menjadi List[ScanResultItem]
    dengan validasi data, pembersihan tipe, serta penandaan needs_verification.
    """
    if not isinstance(raw_items, list):
        return []

    processed: List[ScanResultItem] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue

        nama_produk = clean_str(item.get("nama_produk"))
        harga = clean_numeric(item.get("harga"))
        ukuran = clean_numeric(item.get("ukuran"))
        satuan = clean_str(item.get("satuan"))
        kategori_val = normalize_and_validate_category(item.get("kategori"))

        confidence_val = item.get("confidence")
        if isinstance(confidence_val, str):
            confidence_val = confidence_val.strip().lower()
            if confidence_val not in ("tinggi", "rendah"):
                confidence_val = "rendah"
        else:
            confidence_val = "rendah"

        # Item butuh verifikasi manual jika confidence rendah atau nama/harga belum lengkap
        is_critical_field_missing = (nama_produk is None or harga is None)
        is_any_field_null = is_critical_field_missing or (ukuran is None or satuan is None)
        needs_verification = (confidence_val == "rendah") or is_any_field_null

        processed.append(
            ScanResultItem(
                nama_produk=nama_produk,
                harga=harga,
                ukuran=ukuran,
                satuan=satuan,
                kategori=kategori_val,
                confidence=confidence_val,
                needs_verification=needs_verification
            )
        )

    return processed


def check_needs_escalation(items: List[ScanResultItem]) -> Tuple[bool, str]:
    """
    Evaluasi kelengkapan hasil deteksi dari model gpt-5.6-luna.
    Kembalikan (True, alasan) jika ada indikasi ketidakpastian atau informasi penting
    yang tidak terbaca dengan jelas sehingga perlu verifikasi manual oleh user.
    Jika hasil Luna jelas, lengkap, dan yakin, kembalikan (False, "").
    """
    if not items:
        return True, "Tidak ada produk yang terdeteksi pada hasil scan"

    for idx, item in enumerate(items):
        item_label = item.nama_produk or f"Produk #{idx + 1}"

        # 1. Indikasi ketidakpastian
        if item.confidence.lower() == "rendah":
            return True, f"Item '{item_label}' memiliki confidence rendah"

        # 2. Informasi penting tidak terbaca jelas: nama produk kosong/null
        if not item.nama_produk or not item.nama_produk.strip():
            return True, f"Nama produk untuk item #{idx + 1} tidak terbaca dengan jelas"

        # 3. Informasi penting tidak terbaca jelas: harga kosong/null/<= 0
        if item.harga is None or item.harga <= 0:
            return True, f"Harga produk untuk item '{item_label}' tidak terbaca dengan jelas (null)"

        # 4. needs_verification aktif
        if item.needs_verification:
            return True, f"Item '{item_label}' memiliki atribut yang perlu verifikasi lanjutan"

    return False, ""


async def send_vision_request(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    base64_image: str,
    content_type: str,
    timeout: float = 40.0
) -> dict:
    """
    Mengirim permintaan chat completion bervisi (image input) ke endpoint OpenAI-compatible.
    """
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": user_prompt
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{content_type};base64,{base64_image}"
                        }
                    }
                ]
            }
        ]
    }

    if "qwen" in model.lower():
        payload["reasoning_effort"] = "none"

    logger.info(f"Dispatching vision inference request to model '{model}' at '{url}'...")
    response = await client.post(url, headers=headers, json=payload, timeout=timeout)

    if response.status_code != 200:
        error_detail = response.text
        try:
            err_json = response.json()
            if "error" in err_json and "message" in err_json["error"]:
                err_msg = err_json["error"]["message"]
                if response.status_code == 429 and ("insufficient_quota" in response.text or "credit_balance_exhausted" in response.text):
                    error_detail = f"Saldo kredit API OpenAI habis (Credit balance exhausted): {err_msg}"
                elif response.status_code == 401:
                    error_detail = f"API Key OpenAI tidak valid (Unauthorized): {err_msg}"
                else:
                    error_detail = f"OpenAI API Error ({response.status_code}): {err_msg}"
        except Exception:
            pass
        logger.error(f"Vision API returned status {response.status_code} for model '{model}': {error_detail}")
        raise RuntimeError(error_detail)

    return response.json()


async def _process_groq_fallback(
    client: httpx.AsyncClient,
    groq_api_key: str,
    base64_image: str,
    content_type: str
) -> ScanResponse:
    groq_model = os.getenv("GROQ_MODEL") or "qwen/qwen3.8-27b"
    groq_base_url = (os.getenv("GROQ_BASE_URL") or "https://api.groq.com/openai/v1").rstrip("/")
    try:
        logger.info(f"[Groq Fallback] Processing shelf image using Groq model '{groq_model}'...")
        data = await send_vision_request(
            client=client,
            base_url=groq_base_url,
            api_key=groq_api_key,
            model=groq_model,
            system_prompt=LUNA_SYSTEM_PROMPT,
            user_prompt=LUNA_USER_PROMPT,
            base64_image=base64_image,
            content_type=content_type,
            timeout=30.0
        )
        choices = data.get("choices", [])
        content = choices[0]["message"]["content"] if choices else ""
        parsed = extract_and_parse_json(content)
        raw_items = parsed.get("detected") if isinstance(parsed, dict) else parsed
        items = parse_raw_items(raw_items)
        if not items:
            return ScanResponse(
                detected=[],
                model_used=f"{groq_model} (Groq)",
                escalated_to_verification=False,
                message="Tidak ada produk terdeteksi di rak, coba foto ulang dengan pencahayaan lebih jelas."
            )
        return ScanResponse(
            detected=items,
            model_used=f"{groq_model} (Groq)",
            escalated_to_verification=False,
            message=None
        )
    except Exception as e:
        logger.error(f"[Groq Fallback] Failed: {str(e)}")
        return ScanResponse(
            detected=[],
            model_used=f"{groq_model} (Groq)",
            escalated_to_verification=False,
            message=f"Gagal memproses gambar melalui fallback Groq: {str(e)}"
        )


async def process_shelf_image(
    image_bytes: bytes,
    content_type: str,
    http_client: Optional[httpx.AsyncClient] = None
) -> ScanResponse:
    """
    Pipeline AI Vision:
    - Mode 'groq': Fallback gratis sementara menggunakan Groq LPU (qwen/qwen3.8-27b).
    - Mode 'openai': Single-Pass inferensi cepat menggunakan model gpt-5.6-luna.
      Jika kredit OpenAI habis, otomatis fallback ke Groq agar pemindaian tetap berhasil.
    """
    provider, api_key, base_url, primary_model = get_api_credentials()
    if not api_key or api_key.startswith("your_"):
        logger.error("Vision API Key is not properly configured.")
        return ScanResponse(
            detected=[],
            message=f"API Key untuk provider '{provider}' belum dikonfigurasi di file .env."
        )

    # Encode image ke Base64
    try:
        base64_image = base64.b64encode(image_bytes).decode("utf-8")
    except Exception as e:
        logger.error(f"Failed to encode image to base64: {str(e)}")
        return ScanResponse(
            detected=[],
            message=f"Gagal memproses gambar untuk AI: {str(e)}"
        )

    close_client = False
    client = http_client
    if client is None:
        client = httpx.AsyncClient(timeout=45.0)
        close_client = True

    try:
        # Jika provider aktif adalah Groq (fallback sementara)
        if provider == "groq":
            return await _process_groq_fallback(client, api_key, base64_image, content_type)

        # ---------------------------------------------------------
        # Inferensi langsung menggunakan gpt-5.6-luna
        # ---------------------------------------------------------
        logger.info(f"[Luna Vision] Processing image with model '{primary_model}'...")
        try:
            luna_data = await send_vision_request(
                client=client,
                base_url=base_url,
                api_key=api_key,
                model=primary_model,
                system_prompt=LUNA_SYSTEM_PROMPT,
                user_prompt=LUNA_USER_PROMPT,
                base64_image=base64_image,
                content_type=content_type,
                timeout=35.0
            )
            choices = luna_data.get("choices", [])
            luna_content = choices[0]["message"]["content"] if choices else ""
            parsed_luna = extract_and_parse_json(luna_content)
            raw_luna_items = parsed_luna.get("detected") if isinstance(parsed_luna, dict) else parsed_luna
            luna_items = parse_raw_items(raw_luna_items)
        except Exception as e:
            err_str = str(e)
            groq_key = (os.getenv("GROQ_API_KEY") or "").strip()
            if ("saldo kredit" in err_str.lower() or "credit balance exhausted" in err_str.lower()) and groq_key and not groq_key.startswith("your_"):
                logger.warning("[Auto Fallback] OpenAI credit exhausted ($0). Automatically falling back to Groq LPU...")
                return await _process_groq_fallback(client, groq_key, base64_image, content_type)

            if "saldo kredit" in err_str.lower() or "credit balance exhausted" in err_str.lower() or "unauthorized" in err_str.lower():
                logger.error(f"[Luna Vision] Critical OpenAI error: {err_str}")
                return ScanResponse(
                    detected=[],
                    model_used=primary_model,
                    escalated_to_verification=False,
                    message=err_str
                )
            logger.error(f"[Luna Vision] Luna call failed: {err_str}")
            return ScanResponse(
                detected=[],
                model_used=primary_model,
                escalated_to_verification=False,
                message=f"Gagal memproses gambar: {err_str}"
            )

        if not luna_items:
            return ScanResponse(
                detected=[],
                model_used=primary_model,
                escalated_to_verification=False,
                message="Tidak ada produk terdeteksi di rak, coba foto ulang dengan pencahayaan lebih jelas."
            )

        logger.info(f"[Luna Vision SUCCESS] Model '{primary_model}' resolved {len(luna_items)} items.")
        return ScanResponse(
            detected=luna_items,
            model_used=primary_model,
            escalated_to_verification=False,
            message=None
        )

    finally:
        if close_client:
            await client.aclose()
