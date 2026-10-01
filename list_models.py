"""Fetch the current model list from OmniRoute."""
import sys
from llm_client import OmniRouteError, list_models

def main() -> int:
    print("Mengambil daftar model dari OmniRoute...")
    try:
        models = list_models()
    except (OmniRouteError, ValueError) as exc:
        print(f"Gagal: {exc}")
        print("Periksa OmniRoute, OMNIROUTE_BASE_URL, dan OMNIROUTE_API_KEY.")
        return 1
    ids = sorted({str(item["id"]) for item in models})
    if not ids:
        print("OmniRoute merespons, tetapi tidak ada model.")
        return 1
    with open("all_models.txt", "w", encoding="utf-8") as handle:
        handle.write("\n".join(ids) + "\n")
    print(f"OK: {len(ids)} model ditemukan.")
    print("Disimpan ke all_models.txt")
    for model_id in ids:
        print(f"  {model_id}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
