"""
Termux-friendly 9Router model scanner and latency benchmark.

No LangChain or OpenAI SDK required. Results are resumable in test_state.json.
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from llm_client import APIError, APITransportError, ModelTimeoutError, get_llm, validate_config

LIST_FILE = "all_models.txt"
STATE_FILE = "test_state.json"
REPORT_FILE = "test_results.txt"

PROMPT = "Reply with only one word: OK"
FAST_MAX = 15.0
NORMAL_MAX = 45.0
HARD_LIMIT = 45.0
MAX_ATTEMPTS = 2
BACKOFF = [2.0, 5.0, 10.0]
RETRY_CAP = 15.0
BREAKER_LIMIT = 4
DEFAULT_GAP = 0.35
PROVIDER_GAP = {}
RETRY_STATUSES = {"TRANSIENT", "THROTTLED_SKIP", "TIMEOUT", "EMPTY"}
SAVE_EVERY = 5
KNOWN_COMBOS = set()

BLOCK_RULES = [
    ("invalid_model", ["INVALID_MODEL_ID", "model_not_supported", "model_not_found",
                       "does not exist or you do not have access", "not supported when using Codex",
                       "please check the model you provided", "[404]", "not found for account"]),
    ("retired", ["retired", "end of life", '"Gone"']),
    ("unavailable", ["every channel behind it is switched off"]),
    ("no_credit", ["insufficient_credits", "insufficient balance", "credits required",
                   "requires an active subscription", "not included in your free usage"]),
    ("incompatible", ["unexpected response type", "stream_options", "only available on agentic harnesses",
                      "decisions model", "cannot be used with the chat/completions",
                      "does not support tool calling"]),
    ("auth", ["no active credentials", "api key format is incorrect", "unauthorized",
              "[401]", "[403]", "http 403"]),
]
TRANSIENT_HINTS = [
    "429", "too many requests", "rate limit", "rate-limited", "high demand",
    "temporarily", "overloaded", "resourceexhausted", "[500]", "[502]", "[503]",
    "internal error", "server error", "connection reset", "connection refused",
]

_state_lock = threading.Lock()
_breaker_lock = threading.Lock()
_provider_locks = defaultdict(threading.Lock)
_breaker = defaultdict(int)

GET_TIME_TOOL = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Return a sample time for the requested city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}


class Colors:
    enabled = sys.stdout.isatty() and not os.getenv("NO_COLOR")

    @classmethod
    def paint(cls, text, code):
        return f"\033[{code}m{text}\033[0m" if cls.enabled else text


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def provider_of(model_id):
    return model_id.split("/", 1)[0].lower() if "/" in model_id else "unknown"


def text_of(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(part.get("text") or "")
        return "".join(parts).strip()
    return str(content or "").strip()


def parse_wait(message, headers=None):
    """Read a provider reset hint or HTTP Retry-After header."""
    headers = headers or {}
    for key, value in headers.items():
        if key.lower() in ("retry-after", "x-ratelimit-reset-after", "ratelimit-reset-after"):
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                pass
    match = re.search(r"reset after\s+(?:(\d+)m)?\s*(?:(\d+(?:\.\d+)?)s)?", message, re.I)
    if match and (match.group(1) or match.group(2)):
        return int(match.group(1) or 0) * 60 + float(match.group(2) or 0)
    return None


def classify(exc, message):
    status = getattr(exc, "status_code", None)
    lower = message.lower()

    if isinstance(exc, ModelTimeoutError):
        return "TIMEOUT", "request timeout"
    if status == 402:
        return "BLOCKED", "no_credit"
    if status in (401, 403):
        return "BLOCKED", "auth"
    if status == 404:
        return "BLOCKED", "invalid_model"
    if status == 410:
        return "BLOCKED", "retired"
    if status == 429 or (isinstance(status, int) and status >= 500):
        return "TRANSIENT", "temporary_http_error"

    for reason, needles in BLOCK_RULES:
        if any(needle.lower() in lower for needle in needles):
            return "BLOCKED", reason
    if any(hint in lower for hint in TRANSIENT_HINTS):
        return "TRANSIENT", "temporary_error"
    if isinstance(exc, APITransportError):
        return "TRANSIENT", "network_error"
    return "TRANSIENT", "unknown_error"


def tier_of(latency):
    if latency <= FAST_MAX:
        return "FAST"
    if latency <= NORMAL_MAX:
        return "NORMAL"
    return "SLOW"


def run_probe(model_id, timeout, attempts):
    """Probe one model, retrying transient errors only."""
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            response = get_llm(model_id, read_timeout=timeout).invoke(PROMPT)
            latency = time.monotonic() - started
            answer = text_of(response.content)
            if not answer:
                return {"status": "EMPTY", "reason": "empty_response",
                        "latency": round(latency, 2), "attempts": attempt}
            return {"status": "OK", "tier": tier_of(latency), "latency": round(latency, 2),
                    "detail": answer[:100], "attempts": attempt}
        except Exception as exc:
            message = str(exc)
            status, reason = classify(exc, message)
            latency = round(time.monotonic() - started, 2)
            if status == "TRANSIENT" and attempt < attempts:
                headers = getattr(exc, "headers", {})
                wait = parse_wait(message, headers)
                if wait is None:
                    wait = BACKOFF[min(attempt - 1, len(BACKOFF) - 1)]
                delay = min(max(0.0, wait), RETRY_CAP)
                if delay:
                    time.sleep(delay)
                continue
            return {"status": status, "reason": reason, "detail": message[:240],
                    "latency": latency, "attempts": attempt}
    return {"status": "TRANSIENT", "reason": "unknown_error", "attempts": attempts}


def probe_worker(model_id, timeout, attempts, gap):
    provider = provider_of(model_id)
    with _provider_locks[provider]:
        with _breaker_lock:
            tripped = _breaker[provider] >= BREAKER_LIMIT
        if tripped:
            result = {
                "status": "THROTTLED_SKIP",
                "reason": f"provider {provider} repeatedly rate-limited; skipped for this run",
            }
        else:
            result = run_probe(model_id, timeout, attempts)
            if result["status"] == "TRANSIENT":
                with _breaker_lock:
                    _breaker[provider] += 1
            else:
                with _breaker_lock:
                    _breaker[provider] = 0
            time.sleep(max(0.0, PROVIDER_GAP.get(provider, gap)))
    return model_id, result


def tools_worker(model_id, timeout, gap):
    provider = provider_of(model_id)
    with _provider_locks[provider]:
        try:
            response = get_llm(model_id, read_timeout=timeout).invoke(
                "Use the get_time tool for Jakarta.", tools=[GET_TIME_TOOL]
            )
            outcome = bool(response.tool_calls)
        except Exception:
            outcome = "error"
        time.sleep(max(0.0, PROVIDER_GAP.get(provider, gap)))
    return model_id, outcome


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"meta": {}, "models": {}}
    try:
        with open(STATE_FILE, encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"Gagal membaca {STATE_FILE}: {exc}. Cadangkan file tersebut sebelum menghapusnya."
        )
    if not isinstance(data, dict):
        raise SystemExit(f"Format {STATE_FILE} tidak valid.")
    data.setdefault("meta", {})
    data.setdefault("models", {})
    if not isinstance(data["meta"], dict) or not isinstance(data["models"], dict):
        raise SystemExit(f"Struktur {STATE_FILE} tidak valid.")
    return data


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=1)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, STATE_FILE)


def names_from_report(path):
    names = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as file:
            for line in file:
                text = line.strip()
                if text.startswith("- "):
                    token = text[2:].split()[0].rstrip(":") if text[2:].split() else ""
                    if "/" in token:
                        names.add(token)
    return names


def write_report(state):
    models = state["models"]
    by_status = defaultdict(list)
    for model, result in models.items():
        by_status[result.get("status", "UNKNOWN")].append(model)

    timeout_limit = state.get("meta", {}).get("last_config", {}).get("timeout", HARD_LIMIT)
    lines = [
        f"MODEL TEST REPORT ({now()})",
        f"Recorded: {len(models)} | timeout: {timeout_limit:g}s | "
        f"FAST <= {FAST_MAX:g}s, NORMAL <= {NORMAL_MAX:g}s, SLOW above",
        "",
    ]
    successful = by_status.get("OK", [])
    lines.append(f"OK ({len(successful)}):")
    for tier in ("FAST", "NORMAL", "SLOW"):
        group = sorted(
            (model for model in successful if models[model].get("tier") == tier),
            key=lambda model: (models[model].get("latency", 1e9), model.lower()),
        )
        if group:
            lines.append(f"  -- {tier} ({len(group)}) --")
        for model in group:
            result = models[model]
            extras = []
            if result.get("tools") is True:
                extras.append("tools=YES")
            elif result.get("tools") is False:
                extras.append("tools=NO")
            elif result.get("tools") == "error":
                extras.append("tools=ERROR")
            history = result.get("history", [])
            if len(history) >= 2 and all(value == "OK" for value in history):
                extras.append(f"stable({len(history)}x OK)")
            suffix = ("  " + "  ".join(extras)) if extras else ""
            lines.append(
                f"- {model}  [{result.get('tier')} {result.get('latency')}s]{suffix}"
            )

    sections = [
        ("EMPTY (empty response)", "EMPTY"),
        ("TIMEOUT", "TIMEOUT"),
        ("TRANSIENT (retry later)", "TRANSIENT"),
        ("THROTTLED_SKIP (rate-limited provider skipped)", "THROTTLED_SKIP"),
    ]
    for title, key in sections:
        group = sorted(by_status.get(key, []))
        lines += ["", f"{title} ({len(group)}):"] + [f"- {model}" for model in group]

    blocked = by_status.get("BLOCKED", [])
    lines += ["", f"BLOCKED (action may be needed) ({len(blocked)}):"]
    reasons = defaultdict(list)
    for model in blocked:
        reasons[models[model].get("reason", "unknown")].append(model)
    for reason, group in sorted(reasons.items(), key=lambda item: (-len(item[1]), item[0])):
        lines.append(f"  -- {reason} ({len(group)}) --")
        lines.extend(f"- {model}" for model in sorted(group))

    vanished = state["meta"].get("vanished", [])
    if vanished:
        lines += ["", f"MISSING FROM CURRENT MODEL LIST ({len(vanished)}):"]
        lines.extend(f"- {model}" for model in vanished)

    tmp = REPORT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as file:
        file.write("\n".join(lines) + "\n")
    os.replace(tmp, REPORT_FILE)


def print_summary(state):
    per_provider = defaultdict(lambda: defaultdict(int))
    for model, result in state["models"].items():
        per_provider[provider_of(model)][result.get("status", "UNKNOWN")] += 1

    print("\n=== SUMMARY BY PROVIDER ===")
    print(f"{'provider':<20}{'OK':>5}{'BLOCKED':>9}{'TRANSIENT':>11}{'EMPTY':>7}{'TIMEOUT':>9}{'SKIP':>6}")
    for provider in sorted(per_provider):
        counts = per_provider[provider]
        print(f"{provider:<20}{counts['OK']:>5}{counts['BLOCKED']:>9}"
              f"{counts['TRANSIENT']:>11}{counts['EMPTY']:>7}"
              f"{counts['TIMEOUT']:>9}{counts['THROTTLED_SKIP']:>6}")


def interleave(models):
    """Round-robin provider queues in O(n), avoiding repeated list.pop(0)."""
    queues = defaultdict(deque)
    for model in models:
        queues[provider_of(model)].append(model)
    result = []
    active = deque(queues.values())
    while active:
        queue = active.popleft()
        if queue:
            result.append(queue.popleft())
        if queue:
            active.append(queue)
    return result


def run_pool(items, fn, workers, on_result):
    """Run bounded workers and return quickly on Ctrl+C."""
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [executor.submit(fn, item) for item in items]
        for future in as_completed(futures):
            on_result(*future.result())
    except KeyboardInterrupt:
        # main() saves current progress before exiting; don't block on in-flight network calls.
        executor.shutdown(wait=False)
        raise
    except Exception:
        executor.shutdown(wait=False)
        raise
    else:
        executor.shutdown(wait=True)


def main():
    parser = argparse.ArgumentParser(
        description="Fast, resumable 9Router model scanner (Termux-friendly)"
    )
    parser.add_argument("--all", action="store_true", help="retest every listed model")
    parser.add_argument("--only", default="", help="provider allowlist, comma-separated")
    parser.add_argument("--skip", default="", help="providers to skip, comma-separated")
    parser.add_argument("--tools", action="store_true", help="test tool-calling on successful models")
    parser.add_argument("--workers", type=int, default=4, help="parallel providers (default: 4)")
    parser.add_argument("--timeout", type=float, default=HARD_LIMIT, help="read timeout seconds (default: 45)")
    parser.add_argument("--attempts", type=int, default=MAX_ATTEMPTS, help="attempts per model (default: 2)")
    parser.add_argument("--gap", type=float, default=DEFAULT_GAP, help="seconds between same-provider calls")
    args = parser.parse_args()

    if not 1 <= args.workers <= 32:
        parser.error("--workers must be between 1 and 32")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if not 1 <= args.attempts <= 5:
        parser.error("--attempts must be between 1 and 5")
    if args.gap < 0:
        parser.error("--gap cannot be negative")

    try:
        validate_config()
    except RuntimeError as exc:
        raise SystemExit(str(exc))

    only = {value.strip().lower() for value in args.only.split(",") if value.strip()}
    skip = {value.strip().lower() for value in args.skip.split(",") if value.strip()}

    try:
        with open(LIST_FILE, encoding="utf-8") as file:
            listed = list(dict.fromkeys(line.strip() for line in file if line.strip()))
    except FileNotFoundError:
        raise SystemExit(f"{LIST_FILE} not found. Run: python list_models.py")

    current = [model for model in listed if model not in KNOWN_COMBOS]
    state = load_state()
    meta = state["meta"]
    meta["last_config"] = {
        "workers": args.workers, "timeout": args.timeout,
        "attempts": args.attempts, "gap": args.gap,
    }
    prev_seen = set(meta.get("seen", [])) or names_from_report(REPORT_FILE)
    vanished = (set(meta.get("vanished", [])) | (prev_seen - set(current))) - set(current)
    meta["vanished"] = sorted(vanished)
    meta["seen"] = sorted(current)
    if vanished:
        print(f"Notice: {len(vanished)} previously listed model(s) are no longer in {LIST_FILE}.")

    models_state = state["models"]
    todo = []
    for model in current:
        provider = provider_of(model)
        if (only and provider not in only) or provider in skip:
            continue
        previous = models_state.get(model)
        if args.all or previous is None or previous.get("status") in RETRY_STATUSES:
            todo.append(model)
    todo = interleave(todo)

    print(f"Listed: {len(current)} | recorded: {len(models_state)} | to test: {len(todo)}")
    print(f"Workers: {args.workers} | timeout: {args.timeout:g}s | attempts: {args.attempts} | gap: {args.gap:g}s")
    print(f"Speed tiers: FAST <= {FAST_MAX:g}s | NORMAL <= {NORMAL_MAX:g}s | SLOW above\n")

    counter = {"n": 0}
    save_counter = {"n": 0}

    def checkpoint_if_due():
        save_counter["n"] += 1
        if save_counter["n"] >= SAVE_EVERY:
            save_state(state)
            save_counter["n"] = 0

    def on_probe(model, result):
        counter["n"] += 1
        with _state_lock:
            previous = models_state.get(model, {})
            history = (previous.get("history", []) + [result["status"]])[-3:]
            entry = {**result, "history": history, "tested_at": now()}
            if result["status"] == "OK" and previous.get("tools") is not None:
                entry["tools"] = previous["tools"]
            models_state[model] = entry
            checkpoint_if_due()

        status = result["status"]
        code = "32" if status == "OK" else ("33" if status in ("TRANSIENT", "TIMEOUT", "THROTTLED_SKIP", "EMPTY") else "31")
        label = Colors.paint(f"{status:<14}", code)
        info = f"{result.get('tier')} {result.get('latency')}s" if status == "OK" else result.get("reason", "")
        print(f"[{counter['n']}/{len(todo)}] {model:<54} {label} {info}", flush=True)

    def on_tools(model, outcome):
        with _state_lock:
            if model in models_state:
                models_state[model]["tools"] = outcome
                checkpoint_if_due()
        label = "YES" if outcome is True else "NO" if outcome is False else "ERROR"
        print(f"[tools] {model:<54} {label}", flush=True)

    interrupted = False
    try:
        if todo:
            run_pool(
                todo,
                lambda model: probe_worker(model, args.timeout, args.attempts, args.gap),
                args.workers,
                on_probe,
            )
        if args.tools:
            targets = [
                model for model, result in models_state.items()
                if result.get("status") == "OK" and result.get("tools") is None
                and (not only or provider_of(model) in only) and provider_of(model) not in skip
            ]
            targets = interleave(targets)
            print(f"\nTesting tool-calling for {len(targets)} successful model(s)...")
            run_pool(
                targets,
                lambda model: tools_worker(model, args.timeout, args.gap),
                args.workers,
                on_tools,
            )
    except KeyboardInterrupt:
        interrupted = True
        print("\nStopped. Progress has been saved; run the same command to resume.")
    finally:
        with _state_lock:
            save_state(state)
            write_report(state)

    print_summary(state)
    successful = sum(1 for result in models_state.values() if result.get("status") == "OK")
    print(f"\nSuccessful models: {successful}/{len(models_state)}")
    print(f"Report: {REPORT_FILE} | detailed state: {STATE_FILE}")
    if interrupted:
        sys.stdout.flush()
        os._exit(130)


if __name__ == "__main__":
    main()
