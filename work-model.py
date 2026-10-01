"""Fast terminal dashboard for successfully tested OmniRoute models.

Reads test_state.json. No API requests are made.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime

STATE_FILE = "test_state.json"
OUTPUT_FILE = "work_models.json"

R = "\033[0m"
B = "\033[1m"
C = "\033[96m"
G = "\033[92m"
Y = "\033[93m"
M = "\033[95m"
D = "\033[2m"


def paint(text, code):
    return f"{code}{text}{R}"


def load_state():
    if not os.path.exists(STATE_FILE):
        raise SystemExit(
            f"{STATE_FILE} tidak ditemukan. Jalankan python test_all_models.py dulu."
        )
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Gagal membaca {STATE_FILE}: {exc}")

    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        raise SystemExit(f"Format {STATE_FILE} tidak valid.")
    return data


def build_models(state):
    result = []

    for model, info in state["models"].items():
        if not isinstance(info, dict) or info.get("status") != "OK":
            continue

        latency = info.get("latency")
        if not isinstance(latency, (int, float)):
            continue

        result.append({
            "rank": 0,
            "model": model,
            "provider": model.split("/", 1)[0] if "/" in model else "unknown",
            "latency": float(latency),
            "tier": info.get("tier", "UNKNOWN"),
            "detail": info.get("detail", ""),
            "tested_at": info.get("tested_at"),
            "attempts": info.get("attempts", 1),
            "tools": info.get("tools"),
        })

    result.sort(key=lambda x: (x["latency"], x["model"].lower()))

    for rank, item in enumerate(result, 1):
        item["rank"] = rank

    return result


def save_json(models):
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_work_models": len(models),
        "models": models,
    }
    tmp = OUTPUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUTPUT_FILE)


def main():
    parser = argparse.ArgumentParser(
        description="Scan hasil benchmark dan urutkan model tercepat."
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--provider", default="")
    parser.add_argument("--max-latency", type=float, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.limit < 0 or args.max_latency < 0:
        parser.error("limit/latency tidak boleh negatif")

    state = load_state()
    models = build_models(state)

    if args.provider:
        providers = {x.strip().lower() for x in args.provider.split(",") if x.strip()}
        models = [m for m in models if m["provider"].lower() in providers]

    if args.max_latency:
        models = [m for m in models if m["latency"] <= args.max_latency]

    for rank, item in enumerate(models, 1):
        item["rank"] = rank

    save_json(models)
    shown = models[:args.limit] if args.limit else models

    if args.json:
        print(json.dumps(shown, ensure_ascii=False, indent=2))
        return

    all_ok = build_models(state)
    providers = defaultdict(int)
    for item in all_ok:
        providers[item["provider"]] += 1

    print()
    print(paint("╭─ WORK MODELS / FASTEST ─────────────────────────╮", C))
    print(
        f"│ OK: {len(all_ok):<6} "
        f"shown: {len(shown):<6} "
        f"providers: {len(providers):<3}              │"
    )
    print(paint("╰─────────────────────────────────────────────────╯", C))

    if not shown:
        print(paint("Belum ada model OK yang cocok filter.", Y))
        return

    print()
    print(f"{'#':<4} {'LATENCY':<10} {'TIER':<8} MODEL")
    print("-" * 78)

    for item in shown:
        latency = item["latency"]
        tier = item["tier"]
        code = G if latency <= 8 else Y if latency <= 20 else M
        print(
            f"{item['rank']:<4} "
            f"{paint(f'{latency:.2f}s', code):<19} "
            f"{tier:<8} "
            f"{item['model']}"
        )

    fastest = shown[0]["latency"]
    print()
    print(
        f"Fastest: {paint(f'{fastest:.2f}s', G)}  |  "
        f"JSON: {paint(OUTPUT_FILE, C)}"
    )


if __name__ == "__main__":
    main()
