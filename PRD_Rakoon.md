# Product Requirements Document (PRD)
## Rakoon — AI Computer Vision untuk Belanja Cerdas di Supermarket

**Versi:** 1.0
**Tim:** 3 orang (Mobile/Frontend, Backend & Data, AI/Integration)

---

## 1. Ringkasan Produk

Rakoon adalah aplikasi mobile yang menggunakan kamera smartphone untuk mengenali produk dan membaca harga langsung dari rak supermarket, lalu memberikan rekomendasi produk dengan nilai ekonomi terbaik berdasarkan harga, ukuran kemasan, lokasi toko, dan histori harga.

## 2. Latar Belakang & Masalah

Konsumen sering kesulitan membandingkan nilai ekonomi produk saat belanja, karena perbandingan tidak hanya soal nominal harga tapi juga ukuran kemasan, diskon, lokasi toko, dan tren harga dari waktu ke waktu.

**Rumusan masalah:**
1. Bagaimana membantu konsumen membandingkan nilai ekonomi produk secara instan?
2. Bagaimana memberi rekomendasi produk terbaik berdasarkan harga, ukuran, dan lokasi?
3. Bagaimana membangun basis data harga yang terus diperbarui?

## 3. Tujuan Produk

- Membantu konsumen mengambil keputusan belanja lebih cepat dan cerdas.
- Menampilkan produk dengan nilai (value) terbaik secara real-time.
- Menyediakan riwayat harga berdasarkan lokasi toko.
- Membangun database harga melalui crowdsourcing dari pengguna.

## 4. Target Pengguna

Masyarakat umum, keluarga, mahasiswa, dan pekerja kantoran yang berbelanja rutin di supermarket/minimarket.

## 5. Metrik Keberhasilan (Success Metrics)

> Catatan tim: metrik di bawah adalah baseline awal, sesuaikan setelah user testing.

- Akurasi pengenalan produk oleh AI: **≥ 85%** pada kondisi pencahayaan normal.
- Waktu proses scan-ke-hasil: **≤ 5 detik** per rak.
- Jumlah entri harga baru per minggu (indikator crowdsourcing aktif).
- Retention rate pengguna di minggu ke-4.

## 6. Fitur Utama

### 6.1 Smart Shelf Scan *(MVP — Prioritas 1)*
**Deskripsi:** Pengguna mengarahkan kamera ke rak produk; AI mengenali produk dan membaca label harga sekaligus, tanpa perlu scan barcode satu per satu.

**User story:** Sebagai pengguna, saya ingin memfoto seluruh rak sekaligus dan langsung melihat daftar produk beserta harganya, supaya saya tidak perlu scan satu-satu.

**Acceptance criteria:**
- Sistem dapat mendeteksi minimal 1 produk dalam satu frame foto.
- Sistem menampilkan nama produk, harga, dan ukuran kemasan (jika terbaca) untuk tiap item terdeteksi.
- Jika AI gagal membaca sebagian data (misal harga buram), sistem menandai item tersebut sebagai "perlu verifikasi manual", bukan menampilkan data kosong/salah.
- Pengguna bisa koreksi manual hasil deteksi sebelum data disimpan ke database.

### 6.2 Best Value Recommendation *(MVP — Prioritas 1)*
**Deskripsi:** Sistem menghitung nilai ekonomi tiap produk (harga per satuan ukuran) dan menandai produk dengan value terbaik di antara produk sejenis pada hasil scan.

**Acceptance criteria:**
- Kalkulasi nilai ekonomi = harga ÷ satuan ukuran (per gram/ml/pcs).
- Produk dengan value terbaik ditandai visual jelas (misal badge "Best Value").
- Jika data ukuran kemasan tidak terbaca, produk dikecualikan dari perhitungan (bukan dipaksakan dengan asumsi default).

### 6.3 Price History *(MVP — Prioritas 1)*
**Deskripsi:** Menyimpan dan menampilkan riwayat harga suatu produk di suatu toko dari waktu ke waktu.

**Acceptance criteria:**
- Setiap scan yang berhasil disimpan ke database dengan timestamp + lokasi toko.
- Pengguna bisa melihat grafik/riwayat harga produk tertentu minimal 30 hari terakhir (jika data tersedia).
- Produk baru (belum ada histori) menampilkan status "Belum ada data historis" — bukan grafik kosong yang membingungkan.

### 6.4 Nearby Price Comparison *(Prioritas 2)*
**Deskripsi:** Membandingkan harga produk yang sama di toko-toko terdekat berdasarkan data crowdsourcing.

**Acceptance criteria:**
- Menampilkan daftar toko terdekat (radius default 5km, dapat diubah) yang memiliki data harga untuk produk yang sama.
- Jika data toko lain kosong/tidak cukup, tampilkan pesan bahwa data masih terbatas — bukan hasil kosong tanpa penjelasan.
- **Dependency:** fitur ini baru bernilai guna setelah kepadatan data crowdsourcing memadai (lihat bagian 11 — Fase Pengembangan).

### 6.5 Budget Shopping Assistant *(Prioritas 2)*
**Deskripsi:** Membantu pengguna menyusun daftar belanja dalam batas budget tertentu, mengoptimalkan pilihan produk berdasarkan value terbaik.

**Acceptance criteria:**
- Pengguna input daftar kebutuhan + budget total.
- Sistem menyarankan kombinasi produk (dari hasil scan/database) yang memenuhi kebutuhan dalam budget, diprioritaskan value terbaik.
- **Dependency:** butuh data histori belanja/harga yang cukup matang dari fitur 6.1–6.3 terlebih dulu.

### 6.6 Fake Discount Detection *(Prioritas 3)*
**Deskripsi:** Mendeteksi indikasi diskon yang dinaikkan harganya terlebih dahulu sebelum "didiskon" (predatory pricing).

**Acceptance criteria:**
- Sistem membandingkan harga saat ini dengan histori harga produk yang sama di toko yang sama.
- Jika harga "sebelum diskon" tidak konsisten dengan histori (misal naik drastis dalam waktu singkat sebelum didiskon), tandai sebagai "Perlu verifikasi".
- Sistem **tidak boleh** menampilkan klaim "diskon palsu" secara pasti tanpa data histori minimal (misal minimal 3 titik data harga) — untuk menghindari tuduhan yang tidak berdasar secara hukum.
- **Dependency:** fitur ini paling bergantung pada kematangan data historis (6.3) dan komunitas (6.4). Butuh histori harga yang panjang & reliable untuk akurat.

## 7. Alur Sistem (End-to-End)

1. Pengguna membuka aplikasi.
2. Sistem meminta izin lokasi & kamera (dengan penjelasan alasan penggunaan).
3. GPS mendeteksi lokasi supermarket (via Overpass API, dicocokkan dengan data OpenStreetMap).
4. Pengguna mengarahkan kamera ke rak produk dan mengambil foto.
5. Foto dikirim ke Gemini API untuk pengenalan produk + pembacaan harga (OCR).
6. Sistem mencocokkan hasil dengan database produk/toko yang ada.
7. Sistem menghitung nilai ekonomi tiap produk.
8. Rekomendasi & data terkait ditampilkan ke pengguna secara real-time.
9. Data scan (setelah dikonfirmasi/dikoreksi pengguna) disimpan ke database sebagai kontribusi crowdsourcing.

## 8. Data Model (Level Tinggi)

| Entitas | Atribut Kunci |
|---|---|
| **User** | id, nama, email, reputasi_score |
| **Store** | id, nama, alamat, lat, lng |
| **Product** | id, nama, kategori, ukuran, satuan |
| **PriceEntry** | id, product_id, store_id, harga, sumber_user_id, timestamp, status_verifikasi |
| **ScanSession** | id, user_id, store_id, timestamp, hasil_deteksi (list produk) |

> Catatan: `status_verifikasi` pada PriceEntry penting untuk fitur crowdsourcing — bedakan data yang sudah divalidasi vs masih perlu konfirmasi silang dari user lain.

## 9. Tech Stack

- **Frontend:** Flutter
- **Backend & Database:** Supabase (PostgreSQL, Auth, Storage)
- **AI (Vision + OCR):** Gemini API — pakai model **Flash / Flash-Lite** (gratis via Google AI Studio, tanpa kartu kredit, cukup untuk development & MVP). Model **Pro** berbayar sejak April 2026, dihindari dulu kecuali akurasi Flash tidak cukup.
- **Location:** GPS + OpenStreetMap ecosystem (gratis, tanpa billing):
  - `flutter_map` — render peta
  - Overpass API — pencarian toko/supermarket terdekat (POI)
  - Nominatim — geocoding alamat ↔ koordinat

## 10. Kebutuhan Non-Fungsional

- **Akurasi:** target deteksi produk ≥ 85%, OCR harga ≥ 90% pada foto dengan pencahayaan cukup.
- **Performa:** hasil scan tampil ≤ 5 detik setelah foto diambil (termasuk waktu API call ke Gemini).
- **Privasi & Legal:**
  - Aplikasi harus mencantumkan disclaimer bahwa pengambilan foto di area toko adalah tanggung jawab pengguna, mengikuti kebijakan masing-masing supermarket.
  - Data lokasi pengguna hanya disimpan sebatas store-level, tidak melacak pergerakan personal secara berkelanjutan.
- **Keamanan data crowdsourcing:** perlu mekanisme dasar anti-spam/anti-abuse (misal rate-limit submit harga per user per hari, sistem reputasi sederhana).
- **Offline handling:** aplikasi harus menampilkan pesan jelas jika tidak ada koneksi internet saat scan (bukan macet tanpa keterangan).
- **Kelengkapan data lokasi:** data POI OpenStreetMap di Indonesia belum selengkap Google Maps, terutama untuk toko kecil/minimarket. Perlu fallback: jika toko tidak ditemukan otomatis, pengguna bisa input nama/lokasi toko secara manual.

## 11. Fase Pengembangan

**Fase 1 (MVP):** 6.1 Smart Shelf Scan, 6.2 Best Value Recommendation, 6.3 Price History
**Fase 2:** 6.4 Nearby Price Comparison, 6.5 Budget Shopping Assistant
**Fase 3:** 6.6 Fake Discount Detection

*(Meski disepakati mencakup semua 6 fitur, urutan implementasi tetap mengikuti fase ini karena fitur 6.4–6.6 secara teknis bergantung pada kematangan data dari Fase 1.)*

## 12. Pembagian Tim

- **Mobile/Frontend Dev:** UI Flutter, UX kamera & scanning flow, state management, navigasi.
- **Backend & Data Dev:** Supabase schema, API price history, logic validasi crowdsourcing, manajemen data produk/toko.
- **AI/Integration Dev:** Integrasi Gemini API, logic perhitungan nilai ekonomi, integrasi OpenStreetMap (Overpass API & Nominatim).

## 13. Di Luar Cakupan (Out of Scope) — untuk Iterasi Berikutnya

Integrasi e-wallet, prediksi promo, daftar belanja pintar otomatis, personalisasi rekomendasi berbasis histori individu, integrasi program loyalitas supermarket.
