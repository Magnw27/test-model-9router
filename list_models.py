"""Download the available model IDs from a 9Router OpenAI-compatible endpoint."""
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = (os.getenv("OMNIROUTE_BASE_URL") or os.getenv("NINEROUTER_BASE_URL", "")).strip().rstrip("/")
API_KEY = (os.getenv("OMNIROUTE_API_KEY") or os.getenv("NINEROUTER_API_KEY", "")).strip()
OUTPUT_FILE = "all_models.txt"


def fail(message, code=1):
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def main():
    if not BASE_URL or not API_KEY or API_KEY.lower() in ("isi_api_key_9router_di_sini", "isi_api_key_omniroute_di_sini", "api_key_milikmu"):
        fail("Isi NINEROUTER_BASE_URL dan NINEROUTER_API_KEY di file .env.")
    if not BASE_URL.startswith(("http://", "https://")):
        fail("NINEROUTER_BASE_URL harus diawali http:// atau https://.")

    url = f"{BASE_URL}/models"
    print(f"Connecting to {url} ...")
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"},
            timeout=(8, 25),
        )
    except requests.exceptions.Timeout:
        fail("Koneksi timeout. Pastikan 9Router hidup, port benar, dan server bisa dijangkau.")
    except requests.exceptions.RequestException as exc:
        fail(f"Tidak bisa menghubungi 9Router: {exc}")

    if not response.ok:
        hint = {
            401: "API key salah atau tidak diizinkan.",
            403: "Akses ditolak oleh server.",
            404: "Endpoint /models tidak ditemukan; cek apakah BASE_URL diakhiri /v1.",
            429: "Terlalu banyak permintaan. Coba lagi nanti.",
        }.get(response.status_code, "Periksa alamat server dan log 9Router.")
        fail(f"HTTP {response.status_code}: {response.text[:300]}\n{hint}")

    try:
        data = response.json()
    except ValueError:
        fail("Server tidak mengirim JSON yang valid.")

    raw_models = data.get("data", []) if isinstance(data, dict) else []
    models = []
    seen = set()
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if isinstance(model_id, str) and model_id.strip() and model_id.strip() not in seen:
            model_id = model_id.strip()
            seen.add(model_id)
            models.append(model_id)

    if not models:
        fail("Endpoint berhasil merespons, tetapi tidak ada ID model pada field data.")

    temporary_file = OUTPUT_FILE + ".tmp"
    try:
        with open(temporary_file, "w", encoding="utf-8", newline="\n") as file:
            file.write("\n".join(models) + "\n")
        os.replace(temporary_file, OUTPUT_FILE)
    except OSError as exc:
        fail(f"Gagal menyimpan {OUTPUT_FILE}: {exc}")

    print(f"\nFound {len(models)} unique model(s).")
    print(f"Saved to {OUTPUT_FILE}.")
    print("Preview:")
    for model_id in models[:12]:
        print(f"  - {model_id}")
    if len(models) > 12:
        print(f"  ... and {len(models) - 12} more")


if __name__ == "__main__":
    main()
