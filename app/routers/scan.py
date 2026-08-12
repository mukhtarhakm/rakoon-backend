import os
import base64
import json
import re
import logging
from typing import Optional, List
import httpx
from fastapi import APIRouter, File, UploadFile, HTTPException, status, Depends
from sqlalchemy.orm import Session
from app.models.schemas import ScanResponse, ScanResultItem, ConfirmRequest, ConfirmResponse, ProductCategory, VerificationStatus
from app.models.db_models import Product, PriceEntry
from app.database import get_db

logger = logging.getLogger("rakoon_backend.scan")

router = APIRouter()

def normalize_and_validate_category(val) -> str:
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


def clean_numeric(val) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        val_str = str(val).strip()
        val_str = val_str.replace("Rp", "").replace("rp", "").replace(" ", "")
        return float(val_str)
    except ValueError:
        return None

def clean_str(val) -> Optional[str]:
    if val is None:
        return None
    val_str = str(val).strip()
    if not val_str or val_str.lower() in ("null", "none"):
        return None
    return val_str

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

@router.post("/", response_model=ScanResponse, status_code=status.HTTP_200_OK)
async def scan_shelf_photo(file: UploadFile = File(...)):
    """
    Menerima file foto (multipart/form-data) dan mengirimkannya ke Groq API 
    untuk mendeteksi produk yang ada di rak menggunakan model vision Llama, 
    termasuk nama, harga, ukuran, satuan, dan confidence level.
    """
    # 1. Pastikan API key sudah dikonfigurasi
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key or api_key == "your_groq_api_key_here":
        logger.error("GROQ_API_KEY is not configured in .env file.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Groq API Key belum dikonfigurasi. Silakan tambahkan GROQ_API_KEY ke file .env Anda."
        )

    # 2. Validasi file upload
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nama file tidak valid."
        )
        
    ext = os.path.splitext(file.filename.lower())[1]
    if ext not in ('.jpg', '.jpeg', '.png', '.webp'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File yang diunggah bukan gambar yang didukung (gunakan JPG, JPEG, PNG, atau WEBP)."
        )

    # Membaca konten file
    try:
        contents = await file.read()
        if not contents:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File foto yang diunggah kosong."
            )
        logger.info(f"Received file '{file.filename}' ({len(contents)} bytes) for scanning.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reading file content: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Gagal membaca file foto: {str(e)}"
        )

    # Menentukan MIME type
    content_type = file.content_type
    if not content_type or not content_type.startswith("image/"):
        if ext in ('.jpg', '.jpeg'):
            content_type = "image/jpeg"
        elif ext == '.png':
            content_type = "image/png"
        elif ext == '.webp':
            content_type = "image/webp"
        else:
            content_type = "image/jpeg"

    # 3. Encode image ke Base64
    try:
        logger.info("Encoding image to Base64...")
        base64_image = base64.b64encode(contents).decode("utf-8")
    except Exception as e:
        logger.error(f"Error encoding image to base64: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Gagal memproses gambar untuk dikirim ke AI."
        )

    # 4. Kirim request ke Groq API (OpenAI Compatible Endpoint)
    groq_model = os.getenv("GROQ_MODEL", "qwen/qwen3.6-27b")
    url = "https://api.groq.com/openai/v1/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": groq_model,
        "messages": [
            {
                "role": "system",
                "content": "You are a helpful assistant that only outputs valid JSON. Do not include any explanation, conversational text, or markdown code blocks (like ```json). Output must be strictly valid JSON matching the requested schema."
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Identifikasi produk-produk di rak supermarket yang ada pada foto ini. "
                            "Temukan produk sebanyak-banyaknya yang terdeteksi dengan jelas. "
                            "Untuk setiap produk, kembalikan data berikut:\n"
                            "- nama_produk: nama produk (string, null jika tidak terbaca)\n"
                            "- harga: harga produk (angka/number, null jika tidak terbaca)\n"
                            "- ukuran: ukuran/volume/berat produk (angka/number, null jika tidak terbaca)\n"
                            "- satuan: satuan ukuran seperti ml, gr, kg, pcs, dll. (string, null jika tidak terbaca)\n"
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
                            "- confidence: 'tinggi' jika Anda sangat yakin dengan informasinya, 'rendah' jika ragu-ragu (string)\n\n"
                            "Kembalikan hasilnya dalam format JSON dengan kunci utama bernama 'detected'. "
                            "Contoh output: {\"detected\": [{\"nama_produk\": \"Indomie Mi Goreng\", \"harga\": 3500, \"ukuran\": 85, \"satuan\": \"g\", \"kategori\": \"Makanan Instan\", \"confidence\": \"tinggi\"}]}. "
                            "Jika sama sekali tidak ada produk yang terdeteksi di foto, kembalikan 'detected' sebagai array kosong."
                        )
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

    # Disable thinking tokens for Qwen models to ensure fast response, low token usage, and avoid JSON format errors or timeouts
    if "qwen" in groq_model.lower():
        payload["reasoning_effort"] = "none"


    try:
        logger.info(f"Sending request to Groq API using model '{groq_model}'...")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            
        logger.info(f"Groq API responded with status code {response.status_code}")
            
        if response.status_code != 200:
            logger.error(f"Groq API returned error {response.status_code}: {response.text}")
            return ScanResponse(
                detected=[],
                message=f"Groq API Error ({response.status_code}): Gagal memproses gambar."
            )
            
        groq_data = response.json()
    except httpx.RequestError as e:
        logger.error(f"HTTP request to Groq API failed: {str(e)}")
        return ScanResponse(
            detected=[],
            message="Gagal menghubungi server AI (Connection Timeout/Error)."
        )
    except Exception as e:
        logger.error(f"Unexpected error when calling Groq API: {str(e)}")
        return ScanResponse(
            detected=[],
            message=f"Terjadi kesalahan saat memproses gambar: {str(e)}"
        )

    # 5. Parsing & Validasi Response Groq
    try:
        choices = groq_data.get("choices", [])
        if not choices:
            logger.warning(f"No choices returned from Groq API: {groq_data}")
            return ScanResponse(detected=[], message="Tidak ada produk terdeteksi, coba foto ulang")
            
        text_content = choices[0].get("message", {}).get("content", "")
        if not text_content:
            logger.warning(f"Empty content in Groq response: {groq_data}")
            return ScanResponse(detected=[], message="Tidak ada produk terdeteksi, coba foto ulang")
            
        parsed_json = extract_and_parse_json(text_content)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        logger.error(f"Failed to parse Groq JSON content: {str(e)}")
        return ScanResponse(
            detected=[],
            message="Format data dari AI tidak valid. Pastikan foto cukup jelas dan coba lagi."
        )

    # Ambil daftar item yang terdeteksi
    detected_items = parsed_json.get("detected")
    if not isinstance(detected_items, list):
        if isinstance(parsed_json, list):
            detected_items = parsed_json
        else:
            detected_items = []
            
    logger.info(f"Parsed AI response successfully. Detected {len(detected_items)} items on shelf.")

    if not detected_items:
        return ScanResponse(detected=[], message="Tidak ada produk terdeteksi, coba foto ulang")

    # 6. Pembersihan data & penentuan `needs_verification`
    processed_items: List[ScanResultItem] = []
    for item in detected_items:
        if not isinstance(item, dict):
            continue
            
        nama_produk = clean_str(item.get("nama_produk"))
        harga = clean_numeric(item.get("harga"))
        ukuran = clean_numeric(item.get("ukuran"))
        satuan = clean_str(item.get("satuan"))
        kategori_raw = item.get("kategori")
        kategori_val = normalize_and_validate_category(kategori_raw)
        
        # Normalkan confidence
        confidence_val = item.get("confidence")
        if isinstance(confidence_val, str):
            confidence_val = confidence_val.strip().lower()
            if confidence_val not in ("tinggi", "rendah"):
                confidence_val = "rendah"
        else:
            confidence_val = "rendah"

        # Item butuh verifikasi jika confidence rendah atau ada field penting yang null
        is_any_field_null = (
            nama_produk is None or
            harga is None or
            ukuran is None or
            satuan is None
        )
        needs_verification = (confidence_val == "rendah") or is_any_field_null

        processed_items.append(
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

    if not processed_items:
        return ScanResponse(detected=[], message="Tidak ada produk terdeteksi, coba foto ulang")

    return ScanResponse(detected=processed_items)


@router.post("/confirm", response_model=ConfirmResponse, status_code=status.HTTP_201_CREATED)
def confirm_scan_results(request_data: ConfirmRequest, db: Session = Depends(get_db)):
    """
    Menyimpan hasil scan produk ke database setelah dikonfirmasi atau dikoreksi oleh user di frontend.
    Jika produk belum terdaftar di tabel 'products' (berdasarkan nama case-insensitive), produk baru akan dibuat.
    Setiap entri harga akan disimpan ke tabel 'price_entries' dengan status 'pending'.
    """
    items_saved = 0
    products_created = 0
    
    try:
        for item in request_data.items:
            product_id = None
            clean_name = item.nama_produk.strip()
            
            # 1. Cek apakah produk dengan nama yang sama sudah ada di tabel products (case-insensitive match)
            # Cek di session's new objects terlebih dahulu untuk menghindari duplikasi dalam batch yang sama
            product = None
            for obj in db.new:
                if isinstance(obj, Product) and obj.nama.lower() == clean_name.lower():
                    product = obj
                    break
            
            if not product:
                product = db.query(Product).filter(Product.nama.ilike(clean_name)).first()
            
            if product:
                # Produk sudah ada, ambil product_id-nya
                product_id = product.id
                # Update category only if the existing category is legacy "General"
                if product.kategori.strip().lower() == "general":
                    product.kategori = item.kategori.value if isinstance(item.kategori, ProductCategory) else item.kategori
            else:
                # Produk belum ada, buat produk baru
                new_product = Product(
                    nama=clean_name,
                    ukuran=item.ukuran,
                    satuan=item.satuan,
                    kategori=item.kategori.value if isinstance(item.kategori, ProductCategory) else item.kategori
                )
                db.add(new_product)
                db.flush() # Flush untuk mendapatkan generated ID dari database
                product_id = new_product.id
                products_created += 1
                
            # 2. Insert ke tabel price_entries untuk tiap item
            price_entry = PriceEntry(
                product_id=product_id,
                store_id=str(request_data.store_id),
                harga=item.harga,
                sumber_user_id=str(request_data.user_id),
                status_verifikasi=VerificationStatus.VERIFIED
            )
            db.add(price_entry)
            items_saved += 1
            
        # Commit seluruh perubahan sekaligus
        db.commit()
        
        message = f"Berhasil menyimpan {items_saved} entri harga. Membuat {products_created} produk baru."
        return ConfirmResponse(
            items_saved=items_saved,
            products_created=products_created,
            message=message
        )
        
    except Exception as e:
        db.rollback()
        logger.error(f"Error during scan confirmation: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Terjadi kesalahan saat menyimpan konfirmasi: {str(e)}"
        )
