"""
list_models.py -- Mengambil semua Model ID yang tersedia di akun 9Router-mu
langsung dari endpoint /v1/models (standar OpenAI-compatible API).

Hasilnya disimpan ke all_models.txt (satu Model ID per baris) yang akan
digunakan oleh skrip penguji test_all_models.py.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("NINEROUTER_BASE_URL")
API_KEY = os.getenv("NINEROUTER_API_KEY")


def main():
    if not BASE_URL or not API_KEY:
        raise RuntimeError("NINEROUTER_BASE_URL atau NINEROUTER_API_KEY belum terisi di .env")

    url = f"{BASE_URL.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {API_KEY}"}

    print(f"Menghubungi {url} ...")
    try:
        response = requests.get(url, headers=headers, timeout=30)
    except Exception as e:
        print(f"Gagal menghubungi 9Router: {e}")
        print("Pastikan aplikasi 9Router sudah menyala di background.")
        return

    if response.status_code != 200:
        print(f"Gagal. Status code: {response.status_code}")
        print("Respon:", response.text[:500])
        print(
            "\nKemungkinan endpoint /v1/models tidak tersedia atau API key salah. "
            "Periksa konfigurasi di file .env kamu."
        )
        return

    data = response.json()
    models = [item["id"] for item in data.get("data", [])]

    if not models:
        print("Endpoint merespon, tapi tidak ada model ditemukan di dalamnya.")
        print("Isi mentah respon:", data)
        return

    print(f"Ditemukan {len(models)} model.\n")
    for m in models:
        print(m)

    with open("all_models.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(models))

    print(f"\nDisimpan ke all_models.txt ({len(models)} baris).")


if __name__ == "__main__":
    main()
