#!/usr/bin/env python3
"""Discover and benchmark explicitly identified free models from an OmniRoute-compatible gateway.

Free classification is conservative: a model is included only when the model ID
contains a recognized free suffix, the API marks it free, or pricing metadata
explicitly reports zero input and output cost. Unknown pricing is never assumed free.
"""
import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from dotenv import load_dotenv

from llm_client import API_KEY, BASE_URL, get_llm, validate_config

load_dotenv()

STATE_FILE = "free_test_state.json"
MODEL_FILE = "free_models.txt"
CATALOG_FILE = "free_models_catalog.json"
RANKED_FILE = "free_work_models.txt"
REPORT_FILE = "free_test_results.txt"
PROMPT = "Reply with only one word: OK"
SAVE_EVERY = 5
_state_lock = threading.Lock()


def color(value, code):
    if sys.stdout.isatty() and not os.getenv("NO_COLOR"):
        return f"\033[{code}m{value}\033[0m"
    return value


def is_zero(value):
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def is_explicitly_free(item):
    if not isinstance(item, dict):
        return False, "unknown"
    model_id = str(item.get("id") or "").strip()
    lower_id = model_id.lower()
    if lower_id.endswith(":free") or lower_id.endswith("-free"):
        return True, "model_id_suffix"

    for key in ("free", "is_free"):
        if item.get(key) is True:
            return True, f"metadata_{key}"

    tags = item.get("tags")
    if isinstance(tags, list) and any(str(tag).strip().lower() == "free" for tag in tags):
        return True, "metadata_tag"

    pricing = item.get("pricing")
    if isinstance(pricing, dict):
        input_cost = next((pricing[key] for key in ("prompt", "input", "input_cost") if key in pricing), None)
        output_cost = next((pricing[key] for key in ("completion", "output", "output_cost") if key in pricing), None)
        if input_cost is not None and output_cost is not None and is_zero(input_cost) and is_zero(output_cost):
            return True, "zero_input_output_pricing"

    return False, "not_confirmed_free"


def atomic_json(path, data):
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)


def atomic_lines(path, lines):
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8", newline="\n") as file:
        file.write("\n".join(lines))
        if lines:
            file.write("\n")
    os.replace(temp, path)


def discover_free_models():
    validate_config()
    url = f"{BASE_URL}/models"
    print(f"Fetching model catalog: {url}")
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"},
            timeout=(5, 15),
        )
    except requests.RequestException as exc:
        raise SystemExit(f"Catalog request failed: {exc}")
    if not response.ok:
        hint = " Check the API key and base URL." if response.status_code in (401, 403) else ""
        raise SystemExit(f"Catalog returned HTTP {response.status_code}: {response.text[:250]}{hint}")
    try:
        payload = response.json()
    except ValueError:
        raise SystemExit("The /models endpoint did not return valid JSON.")

    raw = payload.get("data", []) if isinstance(payload, dict) else []
    if not isinstance(raw, list):
        raise SystemExit("Unexpected /models response: data is not a list.")

    free = []
    unknown = 0
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        ok, source = is_explicitly_free(item)
        if ok:
            free.append({"id": model_id, "free_reason": source, "metadata": item})
        else:
            unknown += 1

    free.sort(key=lambda entry: entry["id"].lower())
    if not free:
        raise SystemExit(
            f"No explicitly free models found among {len(seen)} catalog entries. "
            "This gateway may omit pricing/free metadata. The script intentionally does not "
            "assume that an unknown model is free; inspect the gateway's /models response."
        )

    atomic_lines(MODEL_FILE, [item["id"] for item in free])
    atomic_json(CATALOG_FILE, {
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": url,
        "total_models": len(seen),
        "free_count": len(free),
        "unconfirmed_count": unknown,
        "models": free,
    })
    print(f"Catalog: {len(seen)} total | {len(free)} explicitly free | {unknown} not confirmed free")
    print(f"Saved free IDs: {MODEL_FILE}")
    return [item["id"] for item in free]


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"meta": {}, "models": {}}
    try:
        with open(STATE_FILE, encoding="utf-8") as file:
            state = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read {STATE_FILE}: {exc}")
    if not isinstance(state, dict) or not isinstance(state.get("models", {}), dict):
        raise SystemExit(f"Invalid state format in {STATE_FILE}. Back up the file before replacing it.")
    state.setdefault("meta", {})
    state.setdefault("models", {})
    return state


def probe(model_id, timeout, attempts):
    last = None
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            response = get_llm(model_id, read_timeout=timeout).invoke(PROMPT)
            elapsed = round(time.monotonic() - started, 3)
            answer = response.content
            if isinstance(answer, list):
                answer = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in answer)
            answer = str(answer or "").strip()
            if not answer:
                last = {"status": "EMPTY", "reason": "empty_response", "latency": elapsed}
            else:
                return {"status": "OK", "latency": elapsed, "tier": "FAST" if elapsed <= 10 else "NORMAL" if elapsed <= 25 else "SLOW",
                        "detail": answer[:100], "attempts": attempt}
        except Exception as exc:
            elapsed = round(time.monotonic() - started, 3)
            status_code = getattr(exc, "status_code", None)
            if status_code in (401, 402, 403, 404, 410):
                status = "BLOCKED"
            elif isinstance(exc, TimeoutError):
                status = "TIMEOUT"
            else:
                status = "TRANSIENT"
            last = {"status": status, "reason": str(exc)[:220], "latency": elapsed}
        if attempt < attempts and last["status"] in ("TRANSIENT", "TIMEOUT", "EMPTY"):
            time.sleep(min(0.5 * attempt, 1.5))
        else:
            break
    last["attempts"] = attempt
    return last


def main():
    parser = argparse.ArgumentParser(description="Fast benchmark for all explicitly free models in the configured OmniRoute gateway.")
    parser.add_argument("--workers", type=int, default=8, help="parallel model requests (default: 8, max: 32)")
    parser.add_argument("--timeout", type=float, default=12.0, help="per-request read timeout seconds (default: 12)")
    parser.add_argument("--attempts", type=int, default=1, help="attempts per model (default: 1 for speed)")
    parser.add_argument("--all", action="store_true", help="retest models already recorded as successful or blocked")
    parser.add_argument("--no-refresh", action="store_true", help="use the existing free_models.txt instead of fetching /models")
    args = parser.parse_args()
    if not 1 <= args.workers <= 32:
        parser.error("--workers must be between 1 and 32")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if not 1 <= args.attempts <= 3:
        parser.error("--attempts must be between 1 and 3")

    try:
        validate_config()
    except RuntimeError as exc:
        raise SystemExit(str(exc))

    if args.no_refresh:
        if not os.path.isfile(MODEL_FILE):
            raise SystemExit(f"{MODEL_FILE} not found. Run without --no-refresh first.")
        with open(MODEL_FILE, encoding="utf-8") as file:
            model_ids = list(dict.fromkeys(line.strip() for line in file if line.strip()))
    else:
        model_ids = discover_free_models()

    state = load_state()
    results = state["models"]
    todo = [model for model in model_ids if args.all or model not in results or results[model].get("status") not in ("OK", "BLOCKED")]
    print(f"\nFREE MODEL BENCHMARK | {len(model_ids)} models | {len(todo)} to test")
    print(f"Workers={args.workers} | timeout={args.timeout:g}s | attempts={args.attempts}")
    if not todo:
        print("Everything is already tested. Use --all to retest all models.")

    counter = 0
    save_count = 0
    interrupted = False
    pool = ThreadPoolExecutor(max_workers=args.workers)
    futures = {pool.submit(probe, model, args.timeout, args.attempts): model for model in todo}
    try:
        for future in as_completed(futures):
            model = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {"status": "TRANSIENT", "reason": str(exc)[:220]}
            with _state_lock:
                results[model] = {**result, "tested_at": datetime.now().astimezone().isoformat(timespec="seconds")}
                counter += 1
                save_count += 1
                if save_count >= SAVE_EVERY:
                    state["meta"] = {"last_run": datetime.now().astimezone().isoformat(timespec="seconds"),
                                     "workers": args.workers, "timeout": args.timeout, "attempts": args.attempts}
                    atomic_json(STATE_FILE, state)
                    save_count = 0
            status = result.get("status", "UNKNOWN")
            latency = result.get("latency")
            suffix = f"{latency:.3f}s" if isinstance(latency, (int, float)) else result.get("reason", "")
            print(f"[{counter}/{len(todo)}] {model:<54} {color(status, '32' if status == 'OK' else '33' if status in ('TIMEOUT','TRANSIENT','EMPTY') else '31')} {suffix}", flush=True)
    except KeyboardInterrupt:
        interrupted = True
        print("\nStopping; saving completed results...")
    finally:
        pool.shutdown(wait=True)
        with _state_lock:
            state["meta"] = {"last_run": datetime.now().astimezone().isoformat(timespec="seconds"),
                             "workers": args.workers, "timeout": args.timeout, "attempts": args.attempts}
            atomic_json(STATE_FILE, state)

    successful = [
        {"model": model, **result}
        for model, result in results.items()
        if model in model_ids and result.get("status") == "OK" and isinstance(result.get("latency"), (int, float))
    ]
    successful.sort(key=lambda item: (item["latency"], item["model"].lower()))
    atomic_lines(RANKED_FILE, [item["model"] for item in successful])
    lines = [
        f"FREE MODEL REPORT | {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"Discovered: {len(model_ids)} | Successful: {len(successful)}",
        "",
        "FASTEST SUCCESSFUL MODELS:",
    ]
    lines.extend(f"{rank:>4}. {item['latency']:>8.3f}s  {item['model']}" for rank, item in enumerate(successful, 1))
    lines += ["", "OTHER RESULTS:"]
    for model in model_ids:
        result = results.get(model, {})
        if result.get("status") != "OK":
            lines.append(f"- {model} [{result.get('status', 'NOT_TESTED')}] {result.get('reason', '')}")
    atomic_lines(REPORT_FILE, lines)

    print(f"\nSuccessful: {len(successful)}/{len(model_ids)}")
    print(f"Fastest successful models saved to {RANKED_FILE}")
    print(f"Full report: {REPORT_FILE} | State: {STATE_FILE}")
    if interrupted:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
