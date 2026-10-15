# Rakoon Backend

Layanan backend REST API berbasis FastAPI untuk aplikasi **Rakoon** (Rak Pintar & Komparasi Harga Belanja Cerdas). Mengelola pipeline inferensi visi AI (`gpt-5.6-luna`), normalisasi harga per satuan standar (*Best Value*), komparasi harga multi-ritel berbasis geolokasi, optimasi keranjang belanja anggaran, serta modul merchant (klaim toko, pengajuan toko baru, kampanye iklan banner, dan pembayaran via Xendit sandbox).

## Cara Menjalankan

1. **Persiapan Lingkungan (Python 3.11.9):**
   ```bash
   python -m venv .venv
   source .venv/Scripts/activate  # Windows: .venv\Scripts\activate
   # source .venv/bin/activate    # Linux / macOS
   pip install -r requirements.txt
   pip install -r requirements-dev.txt
   ```

2. **Konfigurasi Environment:**
   Salin berkas template dan sesuaikan nilainya:
   ```bash
   cp .env.example .env
   ```

3. **Jalankan Server:**
   ```bash
   uvicorn app.main:app --reload
   ```
   API interaktif Swagger UI tersedia di `http://127.0.0.1:8000/docs`.

4. **Menjalankan Pengujian (Pytest):**
   ```bash
   python -m pytest app/tests -v
   python -m pytest tests -v
   ```

Panduan lengkap mengenai tata cara migrasi database Alembic, pengamanan RLS Supabase, dan konfigurasi deployment Render mengacu pada [SETUP.md](../SETUP.md) di root proyek.
