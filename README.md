# OmniRoute Model Tester — Termux Optimized

Toolkit ringan untuk mengambil daftar model OmniRoute, menguji availability, mengukur latency, mengelompokkan error, retry otomatis, dan opsional mengecek OpenAI-compatible tool calling.

## Kenapa versi ini dibuat ulang?

Versi lama bergantung pada LangChain/OpenAI SDK. Di Termux + Python 3.14, dependency tersebut dapat menarik package native seperti jiter, uuid-utils, maturin, dan Rust.

Versi baru sengaja memakai Python 3.x + requests + python-dotenv + OmniRoute OpenAI-compatible HTTP API.

Tidak membutuhkan LangChain, OpenAI SDK, Rust, atau compiler native.

## Termux

    pkg update
    pkg upgrade
    pkg install python git

Clone:

    cd ~/downloads
    git clone https://github.com/Magnw27/test-model-9router.git
    cd test-model-9router

Update jika sudah ada:

    cd ~/downloads/test-model-9router
    git pull

Install:

    pip install -r requirements.txt

Cek:

    python -c "import requests, dotenv; print('TERMUX OK')"

## Konfigurasi OmniRoute

    cp .env.example .env
    nano .env

Isi:

    OMNIROUTE_BASE_URL=http://localhost:20128/v1
    OMNIROUTE_API_KEY=API_KEY_KAMU

Jika OmniRoute ada di device/server lain:

    OMNIROUTE_BASE_URL=http://IP-SERVER:20128/v1
    OMNIROUTE_API_KEY=API_KEY_KAMU

Jangan pernah commit file .env.

## Ambil model

    python list_models.py

Hasil disimpan ke all_models.txt.

## Benchmark

Default dibuat konservatif untuk HP/Termux:

    python test_all_models.py

Semua model:

    python test_all_models.py --all

Provider tertentu:

    python test_all_models.py --only google,anthropic

Lewati provider:

    python test_all_models.py --skip google

Atur worker:

    python test_all_models.py --workers 2

Termux disarankan 2–4 worker agar lebih ramah terhadap RAM, CPU, koneksi, dan rate limit.

Tool calling:

    python test_all_models.py --tools --workers 2

## Output

all_models.txt — daftar model terbaru.

test_state.json — state benchmark yang disimpan setiap model selesai. Bisa dilanjutkan setelah Termux ditutup.

test_results.txt — laporan yang mudah dibaca.

Status:

- OK — model berhasil merespons.
- EMPTY — server merespons tetapi content kosong.
- TIMEOUT — melewati batas waktu.
- TRANSIENT — error sementara, otomatis dicoba lagi.
- THROTTLED_SKIP — provider sementara dilewati setelah terlalu banyak rate limit.
- BLOCKED — terdeteksi sebagai masalah permanen seperti auth, quota, atau model tidak tersedia.

## Struktur

    test-model-9router/
    ├── .env.example
    ├── .gitignore
    ├── LICENSE
    ├── README.md
    ├── llm_client.py
    ├── list_models.py
    ├── requirements.txt
    └── test_all_models.py

File berikut dibuat lokal dan tidak di-commit:

    all_models.txt
    test_state.json
    test_results.txt

## Keamanan

API key hanya disimpan di .env. Pastikan .env tetap masuk .gitignore.

## License

MIT.