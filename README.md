# 9Router Model Tester & Benchmark

Pemindai dan benchmark model untuk endpoint API yang kompatibel dengan OpenAI di 9Router. Proyek ini dirancang agar tetap ringan di **Termux/Android**, Linux, dan Windows.

## Fitur

- Mengambil daftar model dari endpoint **/models**.
- Menguji respons model dan mengukur latensi.
- Mengurutkan model sukses dari yang tercepat melalui **work-model.py**.
- Menyimpan progres ke **test_state.json** setelah setiap model, sehingga pengujian dapat dilanjutkan.
- Memisahkan error permanen, timeout, error sementara, dan respons kosong.
- Mengurangi request bersamaan ke provider yang sama, melakukan retry terbatas, dan melewati provider sementara setelah beberapa error beruntun.
- Pengujian tool-calling opsional.
- Output terminal berwarna tanpa dependensi warna tambahan.

## Mengapa lebih cocok untuk Termux?

Klien API menggunakan **requests** secara langsung. LangChain dan OpenAI SDK tidak lagi dibutuhkan, sehingga instalasi lebih sederhana dan menghindari dependensi tambahan seperti **jiter** yang dapat bermasalah di beberapa environment Python/Android.

## Instalasi di Termux

Jalankan satu per satu:

~~~bash
pkg update
pkg install python git nano -y
git clone https://github.com/Magnw27/test-model-9router.git
cd test-model-9router
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
nano .env
~~~

Isi konfigurasi di dalam **.env**:

~~~env
NINEROUTER_BASE_URL=http://127.0.0.1:20128/v1
NINEROUTER_API_KEY=API_KEY_MILIKMU
~~~

**Penting tentang alamat server:** **127.0.0.1** hanya mengarah ke perangkat yang menjalankan skrip. Jika 9Router berjalan di PC lain, ganti host dengan IP lokal PC tersebut, misalnya **http://192.168.1.10:20128/v1**, lalu pastikan firewall dan konfigurasi server mengizinkan koneksi dari perangkatmu. Port **20128** adalah contoh; gunakan port API yang benar dari konfigurasi 9Router-mu.

Jangan mengunggah file **.env** atau membagikan API key. File **.env** sudah masuk **.gitignore**.

## Cara menjalankan

### 1. Ambil daftar model terbaru

~~~bash
python list_models.py
~~~

Daftar ID model disimpan ke **all_models.txt**.

### 2. Mulai pengujian

~~~bash
python test_all_models.py
~~~

Pengaturan default dibuat cukup ramah untuk ponsel: 4 worker, timeout 45 detik, maksimal 2 percobaan, serta jeda singkat antar-request dalam provider yang sama.

Contoh opsi:

~~~bash
# Lebih ringan untuk koneksi/ponsel yang lambat
python test_all_models.py --workers 2 --timeout 30

# Lebih cepat, tetapi berpotensi lebih mudah terkena rate limit
python test_all_models.py --workers 6 --timeout 25 --attempts 1 --gap 0.2

# Uji ulang semua model
python test_all_models.py --all

# Uji provider tertentu atau lewati provider tertentu
python test_all_models.py --only google,openai
python test_all_models.py --skip openrouter,groq

# Tambahkan pengujian tool-calling untuk model yang sukses
python test_all_models.py --tools
~~~

Pilihan **--workers** menerima 1–32. Untuk Termux, mulai dari 2 atau 4, lalu naikkan perlahan jika koneksi stabil. Worker paralel bukan berarti provider yang sama akan dibombardir bersamaan: model dalam provider yang sama tetap dijalankan bergantian.

Jika proses dihentikan dengan Ctrl+C, progres yang sudah selesai disimpan. Jalankan perintah yang sama kembali untuk melanjutkan. Status **TRANSIENT** dan **THROTTLED_SKIP** dicoba kembali pada eksekusi berikutnya; gunakan **--all** untuk memaksa tes ulang semua model.

### 3. Tampilkan model yang berhasil dan paling cepat

~~~bash
python work-model.py
~~~

Perintah ini membaca **test_state.json**, menampilkan model berstatus **OK** dari latensi tercepat ke terlambat, lalu mengekspor:

- **work_models.txt** — daftar ID model terurut, satu model per baris.
- **work_models.json** — detail latensi, provider, status kecepatan, dan hasil tool-calling.

Opsi tambahan:

~~~bash
# Hanya tampilkan 20 model tercepat
python work-model.py --limit 20

# Filter berdasarkan provider
python work-model.py --provider google,openai

# Hanya model yang terbukti mendukung tool-calling
python work-model.py --tools

# Tampilkan tanpa membuat file hasil
python work-model.py --no-export
~~~

## File hasil

| File | Kegunaan |
| --- | --- |
| all_models.txt | Daftar model yang diambil dari API |
| test_state.json | Status, latensi, riwayat tes, dan waktu pengujian |
| test_results.txt | Laporan teks hasil seluruh pengujian |
| work_models.txt | Model sukses, diurutkan dari tercepat |
| work_models.json | Data benchmark terstruktur |

File hasil lokal tersebut diabaikan oleh Git dan tidak ikut terunggah.

## Arti status

- **OK** — model mengembalikan respons yang tidak kosong.
- **BLOCKED** — kemungkinan memerlukan perbaikan konfigurasi, akses, kredit, atau model sudah tidak tersedia.
- **TRANSIENT** — error sementara, misalnya rate limit atau server sibuk; akan dicoba kembali pada pengujian berikutnya.
- **TIMEOUT** — request mencapai batas waktu tunggu.
- **EMPTY** — server merespons, tetapi teks balasan kosong.
- **THROTTLED_SKIP** — model dilewati sementara karena provider mengalami beberapa error sementara berturut-turut.

Kategori latensi: **FAST** sampai 15 detik, **NORMAL** sampai 45 detik, dan **SLOW** di atas 45 detik. Pengelompokan ini mengukur respons saat pengujian, bukan jaminan performa di masa mendatang. Hasil juga bergantung pada koneksi, antrean provider, kuota, dan konfigurasi API yang sedang digunakan.

## Dependensi

Hanya dua dependensi Python:

- **requests**
- **python-dotenv**

## Lisensi

MIT.