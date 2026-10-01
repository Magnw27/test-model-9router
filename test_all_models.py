"""Fast Termux-first OmniRoute model benchmark."""
from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from llm_client import chat_completion

LIST_FILE = "all_models.txt"
STATE_FILE = "test_state.json"
REPORT_FILE = "test_results.txt"

PROMPT = "Balas hanya dengan satu kata: OK"
FAST_MAX = 8.0
NORMAL_MAX = 20.0
HARD_LIMIT = 30.0

DEFAULT_WORKERS = 8
MAX_WORKERS = 16
MAX_ATTEMPTS = 2
BACKOFF = (1.0, 3.0)
RETRY_CAP = 5.0
BREAKER_LIMIT = 4
DEFAULT_GAP = 0.10

RETRY_STATUSES = {"TRANSIENT", "THROTTLED_SKIP"}
KNOWN_COMBOS = set()

BLOCK_RULES = [
    ("invalid_model", (
        "invalid_model_id", "model_not_supported", "model_not_found",
        "does not exist or you do not have access", "please check the model",
        "[404]", "not found for account", "unknown model"
    )),
    ("retired", ("retired", "end of life", "deprecated")),
    ("unavailable", ("every channel behind it is switched off",)),
    ("no_credit", (
        "insufficient_credits", "insufficient balance", "credits required",
        "requires an active subscription", "not included in your free usage"
    )),
    ("incompatible", (
        "unexpected response type", "only available on agentic harnesses",
        "decisions model", "cannot be used with the chat/completions"
    )),
    ("auth", (
        "no active credentials", "api key format is incorrect",
        "unauthorized", "[401]", "[403]", "http 403"
    )),
]

TRANSIENT_HINTS = (
    "429", "too many requests", "rate limit", "rate-limited", "high demand",
    "temporarily", "overloaded", "resourceexhausted", "[500]", "[502]",
    "[503]", "internal error", "service unavailable", "gateway timeout"
)

_state_lock = threading.Lock()
_breaker_lock = threading.Lock()
_provider_locks = defaultdict(threading.Lock)
_breaker = defaultdict(int)


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    MAGENTA = "\033[95m"
    DIM = "\033[2m"


def color(text, code):
    return f"{code}{text}{Colors.RESET}"


def now():
    return __import__("datetime").datetime.now().isoformat(timespec="seconds")


def provider_of(model_id):
    return model_id.split("/", 1)[0]


def text_of(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            p if isinstance(p, str) else str(p.get("text") or "")
            for p in content if isinstance(p, (str, dict))
        ).strip()
    return str(content or "").strip()


def parse_wait(msg):
    m = re.search(r"reset after\s+(?:(\d+)m)?\s*(?:(\d+)s)?", msg, re.I)
    return None if not m else int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def classify(exc, msg):
    low = msg.lower()
    if isinstance(exc, (requests.Timeout, TimeoutError)) or "timed out" in low or "timeout" in low:
        return "TIMEOUT", "timeout"

    for reason, needles in BLOCK_RULES:
        if any(n in low for n in needles):
            return "BLOCKED", reason

    if any(n in low for n in TRANSIENT_HINTS):
        return "TRANSIENT", None

    return "TRANSIENT", "unknown"


def tier_of(latency):
    if latency <= FAST_MAX:
        return "FAST"
    if latency <= NORMAL_MAX:
        return "NORMAL"
    return "SLOW"


def run_probe(model_id):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            data = chat_completion(
                model_id,
                [{"role": "user", "content": PROMPT}],
                timeout=HARD_LIMIT,
            )
            latency = time.monotonic() - started
            choices = data.get("choices") or []
            message = choices[0].get("message", {}) if choices else {}
            response_text = text_of(message.get("content", ""))

            if not response_text and not message.get("tool_calls"):
                return {
                    "status": "EMPTY",
                    "reason": "balasan kosong",
                    "latency": round(latency, 2),
                    "attempts": attempt,
                }

            return {
                "status": "OK",
                "tier": tier_of(latency),
                "latency": round(latency, 2),
                "detail": response_text[:60],
                "attempts": attempt,
            }

        except Exception as exc:
            msg = str(exc)
            kind, reason = classify(exc, msg)
            latency = round(time.monotonic() - started, 2)

            if kind == "TRANSIENT" and attempt < MAX_ATTEMPTS:
                wait = min(parse_wait(msg) or BACKOFF[attempt - 1], RETRY_CAP)
                time.sleep(wait)
                continue

            return {
                "status": kind,
                "reason": reason,
                "detail": msg[:300],
                "latency": latency,
                "attempts": attempt,
            }

    return {"status": "TRANSIENT", "reason": "unknown", "attempts": MAX_ATTEMPTS}


def probe_worker(model_id):
    provider = provider_of(model_id)

    with _provider_locks[provider]:
        with _breaker_lock:
            tripped = _breaker[provider] >= BREAKER_LIMIT

        if tripped:
            return model_id, {
                "status": "THROTTLED_SKIP",
                "reason": f"provider {provider} terlalu sering rate-limit",
            }

        result = run_probe(model_id)
        time.sleep(DEFAULT_GAP)

        with _breaker_lock:
            if result["status"] == "TRANSIENT":
                _breaker[provider] += 1
            else:
                _breaker[provider] = 0

    return model_id, result


TOOL_SCHEMA = [{
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Return the current time for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}]


def tools_worker(model_id):
    provider = provider_of(model_id)
    with _provider_locks[provider]:
        try:
            data = chat_completion(
                model_id,
                [{"role": "user", "content": "Gunakan tool get_time untuk kota Jakarta."}],
                timeout=HARD_LIMIT,
                tools=TOOL_SCHEMA,
            )
            message = ((data.get("choices") or [{}])[0]).get("message", {})
            outcome = bool(message.get("tool_calls"))
        except Exception as exc:
            print(color(f"[tools] {model_id}: {str(exc)[:140]}", Colors.RED))
            outcome = "error"
        time.sleep(DEFAULT_GAP)
    return model_id, outcome


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"meta": {}, "models": {}}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError
        data.setdefault("meta", {})
        data.setdefault("models", {})
        return data
    except (OSError, ValueError, json.JSONDecodeError):
        print(color("State rusak; memulai state baru.", Colors.YELLOW))
        return {"meta": {}, "models": {}}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def write_report(state):
    groups = defaultdict(list)
    for model, result in state["models"].items():
        groups[result.get("status", "UNKNOWN")].append(model)

    lines = [
        f"OMNIROUTE MODEL BENCHMARK — {now()}",
        f"Total: {len(state['models'])}",
        f"FAST <= {FAST_MAX:g}s | NORMAL <= {NORMAL_MAX:g}s | SLOW > {NORMAL_MAX:g}s | TIMEOUT > {HARD_LIMIT:g}s",
        "",
    ]

    for status in ("OK", "EMPTY", "TIMEOUT", "TRANSIENT", "THROTTLED_SKIP", "BLOCKED"):
        items = sorted(groups.get(status, []))
        lines.append(f"{status} ({len(items)})")
        for model in items:
            r = state["models"][model]
            if status == "OK":
                lines.append(f"- {model} | {r.get('tier')} | {r.get('latency')}s")
            else:
                lines.append(f"- {model} | {r.get('reason') or r.get('detail') or '-'}")
        lines.append("")

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def print_summary(state, elapsed):
    counts = defaultdict(int)
    latencies = []
    for r in state["models"].values():
        counts[r.get("status", "UNKNOWN")] += 1
        if r.get("status") == "OK" and isinstance(r.get("latency"), (int, float)):
            latencies.append(r["latency"])

    print()
    print(color("=== BENCHMARK SELESAI ===", Colors.BOLD + Colors.CYAN))
    print(
        f"OK {color(str(counts['OK']), Colors.GREEN)}  "
        f"BLOCKED {color(str(counts['BLOCKED']), Colors.RED)}  "
        f"TRANSIENT {color(str(counts['TRANSIENT']), Colors.YELLOW)}  "
        f"TIMEOUT {color(str(counts['TIMEOUT']), Colors.MAGENTA)}"
    )

    if latencies:
        fastest = min(latencies)
        avg = sum(latencies) / len(latencies)
        print(f"Fastest: {color(f'{fastest:.2f}s', Colors.GREEN)} | Average: {avg:.2f}s")

    print(f"Elapsed: {elapsed:.1f}s")
    print(f"Report : {REPORT_FILE}")
    print(f"State  : {STATE_FILE}")


def interleave(models):
    queues = defaultdict(list)
    for model in models:
        queues[provider_of(model)].append(model)

    out = []
    while any(queues.values()):
        for queue in queues.values():
            if queue:
                out.append(queue.pop(0))
    return out


def run_pool(items, fn, workers, on_result):
    total = len(items)
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(fn, item) for item in items]

        for future in as_completed(futures):
            on_result(*future.result())
            done += 1

            if done % 10 == 0 or done == total:
                print(
                    color(
                        f"  progress {done}/{total} ({done / total * 100:.1f}%)",
                        Colors.DIM,
                    )
                )


def main():
    parser = argparse.ArgumentParser(description="Fast OmniRoute benchmark for Termux")
    parser.add_argument("--all", action="store_true", help="uji ulang semua model")
    parser.add_argument("--only", default="", help="provider saja, contoh: openai,google")
    parser.add_argument("--skip", default="", help="provider yang dilewati")
    parser.add_argument("--tools", action="store_true", help="uji tool calling setelah probe")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()

    if not 1 <= args.workers <= MAX_WORKERS:
        parser.error(f"--workers harus 1-{MAX_WORKERS}")

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    skip = {x.strip() for x in args.skip.split(",") if x.strip()}

    try:
        with open(LIST_FILE, encoding="utf-8") as f:
            current = [
                x.strip() for x in f
                if x.strip() and x.strip() not in KNOWN_COMBOS
            ]
    except FileNotFoundError:
        raise SystemExit("all_models.txt tidak ditemukan. Jalankan: python list_models.py")

    state = load_state()
    models_state = state["models"]
    previous = set(state["meta"].get("seen", []))
    state["meta"]["vanished"] = sorted(previous - set(current))
    state["meta"]["seen"] = sorted(current)

    todo = []
    for model in current:
        provider = provider_of(model)
        prev = models_state.get(model)

        if (only and provider not in only) or provider in skip:
            continue

        if args.all or prev is None or prev.get("status") in RETRY_STATUSES:
            todo.append(model)

    todo = interleave(todo)

    print(color("╭─ OMNIROUTE FAST SCANNER ─────────────────────╮", Colors.CYAN))
    print(
        f"│ models: {len(current):<8} "
        f"scan: {len(todo):<8} workers: {args.workers:<3}       │"
    )
    print(
        f"│ timeout: {HARD_LIMIT:g}s  fast <= {FAST_MAX:g}s  "
        f"retry: {MAX_ATTEMPTS}                   │"
    )
    print(color("╰──────────────────────────────────────────────╯", Colors.CYAN))

    if not todo:
        print(color("Tidak ada model baru untuk dites.", Colors.YELLOW))

    counter = 0
    started_all = time.monotonic()

    def on_probe(model, result):
        nonlocal counter
        counter += 1

        with _state_lock:
            old = models_state.get(model, {})
            models_state[model] = {
                **result,
                "history": (old.get("history", []) + [result["status"]])[-5:],
                "tested_at": now(),
            }
            if "tools" in old and result["status"] == "OK":
                models_state[model]["tools"] = old["tools"]
            save_state(state)

        status = result["status"]
        if status == "OK":
            msg = f"[{counter}/{len(todo)}] {model} -> OK {result['latency']:.2f}s {result['tier']}"
            print(color(msg, Colors.GREEN))
        elif status == "BLOCKED":
            print(color(f"[{counter}/{len(todo)}] {model} -> BLOCKED {result.get('reason')}", Colors.RED))
        elif status == "TIMEOUT":
            print(color(f"[{counter}/{len(todo)}] {model} -> TIMEOUT", Colors.MAGENTA))
        else:
            print(color(f"[{counter}/{len(todo)}] {model} -> {status} {result.get('reason') or ''}", Colors.YELLOW))

    def on_tools(model, outcome):
        with _state_lock:
            models_state[model]["tools"] = outcome
            save_state(state)
        print(f"[tools] {model} -> {outcome}")

    try:
        if todo:
            run_pool(todo, probe_worker, args.workers, on_probe)

        if args.tools:
            targets = [
                m for m, r in models_state.items()
                if r.get("status") == "OK"
                and r.get("tools") is None
                and (not only or provider_of(m) in only)
                and provider_of(m) not in skip
            ]
            if targets:
                print(color(f"Tool calling: {len(targets)} model", Colors.CYAN))
                run_pool(interleave(targets), tools_worker, args.workers, on_tools)

    except KeyboardInterrupt:
        print(color("\nDihentikan. State sudah tersimpan.", Colors.YELLOW))
    finally:
        with _state_lock:
            save_state(state)
            write_report(state)

    print_summary(state, time.monotonic() - started_all)


if __name__ == "__main__":
    raise SystemExit(main())
