"""Fetch all models exposed by OmniRoute via its OpenAI-compatible API."""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("OMNIROUTE_BASE_URL", "http://localhost:20128/v1").rstrip("/")
API_KEY = os.getenv("OMNIROUTE_API_KEY")


def main():
    if not API_KEY:
        raise RuntimeError("OMNIROUTE_API_KEY belum terisi di .env")

    url = f"{BASE_URL}/models"
    headers = {"Authorization": f"Bearer {API_KEY}"}

    print(f"Menghubungi OmniRoute: {url} ...")
    try:
        response = requests.get(url, headers=headers, timeout=30)
    except Exception as e:
        print(f"Gagal menghubungi OmniRoute: {e}")
        print("Pastikan server OmniRoute sudah berjalan dan URL-nya benar.")
        return

    if response.status_code != 200:
        print(f"Gagal. Status code: {response.status_code}")
        print("Respon:", response.text[:500])
        print("Periksa OMNIROUTE_BASE_URL dan OMNIROUTE_API_KEY di .env.")
        return

    data = response.json()
    models = [item["id"] for item in data.get("data", []) if item.get("id")]

    if not models:
        print("Endpoint merespon, tapi tidak ada model ditemukan.")
        print("Isi mentah respon:", data)
        return

    print(f"Ditemukan {len(models)} model.\\n")
    for model in models:
        print(model)

    with open("all_models.txt", "w", encoding="utf-8") as f:
        f.write("\\n".join(models))

    print(f"\\nDisimpan ke all_models.txt ({len(models)} baris).")


if __name__ == "__main__":
    main()
