"""
test_all_models.py -- Uji ketersediaan dan kapabilitas model di OmniRoute.

Fitur:
1. Waktu respons dikelompokkan: FAST (<=15s) / NORMAL (<=45s) / SLOW (>45s). Lebih dari 2 menit = TIMEOUT.
2. Kegagalan dibedakan secara detail:
   - BLOCKED (permanen: kredit habis, model retired, auth rusak, dsb)
   - TRANSIENT (sementara: rate limit 429, server sibuk). TRANSIENT dicoba ulang otomatis
     dan bisa diuji lagi nanti tanpa mengulang seluruh pengujian dari awal.
3. Selang-seling antar provider (interleaving) untuk mencegah ban / rate limit beruntun:
   1 request per provider dalam satu waktu, jeda otomatis, dan sistem circuit breaker.
4. Opsional --tools: cek apakah model yang OK mendukung function calling / tool calling.
5. Progres tersimpan secara aman & atomik di test_state.json setiap kali model selesai diuji.
   Laporan yang mudah dibaca ditulis di test_results.txt.

Cara pakai:
  python list_models.py              (ambil daftar model terbaru dari OmniRoute)
  python test_all_models.py          (uji yang belum diuji + ulang yang TRANSIENT)
  python test_all_models.py --all    (uji ulang SEMUA model)
  python test_all_models.py --only kr,ag     (hanya uji provider tertentu)
  python test_all_models.py --skip cl,af     (lewati provider tertentu)
  python test_all_models.py --tools          (tambah uji tool-calling untuk model yang OK)
"""

import argparse
import json
import os
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import openai
from langchain_core.tools import tool

from llm_client import get_llm

# ---------------------------------------------------------------- pengaturan
LIST_FILE = "all_models.txt"
STATE_FILE = "test_state.json"
REPORT_FILE = "test_results.txt"

PROMPT = "Balas hanya dengan satu kata: OK"

FAST_MAX = 15.0      # detik: sampai sini = FAST
NORMAL_MAX = 45.0    # detik: sampai sini = NORMAL, di atasnya = SLOW
HARD_LIMIT = 120.0   # detik: lebih dari ini = TIMEOUT (dianggap gagal)

MAX_ATTEMPTS = 3               # percobaan untuk error TRANSIENT
BACKOFF = [5.0, 15.0, 30.0]    # jeda jika server tidak memberi petunjuk "reset after"
RETRY_CAP = 45.0               # jeda tunggu maksimum antar percobaan
BREAKER_LIMIT = 8              # provider dihentikan sementara bila segini model gagal TRANSIENT
DEFAULT_GAP = 1.0              # jeda (detik) setelah tiap request ke provider yang sama
PROVIDER_GAP = {"af": 2.0}     # provider dengan batas ketat
RETRY_STATUSES = {"TRANSIENT", "THROTTLED_SKIP"}

# Daftar combo ID atau nama model custom yang ingin dilewati (opsional)
KNOWN_COMBOS = set()

# Aturan pendeteksian kegagalan permanen (butuh tindakan user / model memang tidak ada)
BLOCK_RULES = [
    ("invalid_model", ["INVALID_MODEL_ID", "model_not_supported", "model_not_found",
                       "does not exist or you do not have access", "not supported when using Codex",
                       "please check the model you provided", "[404]", "not found for account"]),
    ("retired", ["retired", "end of life", '"Gone"']),
    ("unavailable", ["every channel behind it is switched off"]),
    ("no_credit", ["insufficient_credits", "Insufficient Balance", "Credits Required",
                   "requires an active subscription", "not included in your free usage"]),
    ("incompatible", ["Unexpected response type", "stream_options", "only available on agentic harnesses",
                      "decisions model", "cannot be used with the chat/completions"]),
    ("auth", ["No active credentials", "API key format is incorrect", "Unauthorized",
              "[401]", "[403]", "HTTP 403"]),
]

# Kata kunci kegagalan sementara (layak dicoba lagi)
TRANSIENT_HINTS = ["429", "Too Many Requests", "rate limit", "rate-limited", "high demand",
                   "temporarily", "overloaded", "ResourceExhausted", "[500]", "[502]", "[503]",
                   "Internal error"]

_state_lock = threading.Lock()
_breaker_lock = threading.Lock()
_provider_locks = defaultdict(threading.Lock)
_breaker = defaultdict(int)


@tool
def get_time(city: str) -> str:
    """Ambil jam saat ini untuk sebuah kota."""
    return "12:00"


# ---------------------------------------------------------------- utilitas
def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def provider_of(model_id: str) -> str:
    return model_id.split("/", 1)[0]


def text_of(content) -> str:
    """Ambil teks dari balasan model, baik berbentuk string maupun list of parts."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                parts.append(p.get("text") or "")
        return "".join(parts).strip()
    return str(content or "").strip()


def parse_wait(msg: str):
    """Membaca petunjuk '(reset after 1m 16s)' dari pesan error."""
    m = re.search(r"reset after\s+(?:(\d+)m)?\s*(?:(\d+)s)?", msg)
    if m and (m.group(1) or m.group(2)):
        return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    return None


def classify(exc, msg: str):
    low = msg.lower()
    if isinstance(exc, openai.APITimeoutError) or "timed out" in low:
        return "TIMEOUT", "lebih dari batas waktu"
    for reason, needles in BLOCK_RULES:
        if any(n.lower() in low for n in needles):
            return "BLOCKED", reason
    if any(h.lower() in low for h in TRANSIENT_HINTS):
        return "TRANSIENT", None
    return "TRANSIENT", "unknown"


def tier_of(latency: float) -> str:
    if latency <= FAST_MAX:
        return "FAST"
    if latency <= NORMAL_MAX:
        return "NORMAL"
    return "SLOW"


# ---------------------------------------------------------------- pengujian
def run_probe(model_id: str) -> dict:
    """Uji satu model. Mencoba ulang otomatis jika terkena error TRANSIENT."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        t0 = time.monotonic()
        try:
            resp = get_llm(model_id, read_timeout=HARD_LIMIT).invoke(PROMPT)
            latency = time.monotonic() - t0
            if latency > HARD_LIMIT:
                return {"status": "TIMEOUT", "reason": "lebih dari batas waktu",
                        "latency": round(latency, 1), "attempts": attempt}
            text = text_of(resp.content)
            if not text:
                return {"status": "EMPTY", "reason": "balasan kosong",
                        "latency": round(latency, 1), "attempts": attempt}
            return {"status": "OK", "tier": tier_of(latency), "latency": round(latency, 1),
                    "detail": text[:60], "attempts": attempt}
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            kind, reason = classify(e, msg)
            latency = round(time.monotonic() - t0, 1)
            if kind == "TRANSIENT" and attempt < MAX_ATTEMPTS:
                wait = parse_wait(msg)
                wait = BACKOFF[attempt - 1] if wait is None else wait
                time.sleep(min(wait, RETRY_CAP))
                continue
            return {"status": kind, "reason": reason, "detail": msg[:200],
                    "latency": latency, "attempts": attempt}
    return {"status": "TRANSIENT", "reason": "unknown", "attempts": MAX_ATTEMPTS}


def probe_worker(model_id: str):
    provider = provider_of(model_id)
    with _provider_locks[provider]:  # 1 request per provider dalam satu waktu
        with _breaker_lock:
            tripped = _breaker[provider] >= BREAKER_LIMIT
        if tripped:
            result = {"status": "THROTTLED_SKIP",
                      "reason": f"provider {provider} terus kena rate limit, dilewati sementara"}
        else:
            result = run_probe(model_id)
            time.sleep(PROVIDER_GAP.get(provider, DEFAULT_GAP))
            if result["status"] == "TRANSIENT":
                with _breaker_lock:
                    _breaker[provider] += 1
    return model_id, result


def tools_worker(model_id: str):
    provider = provider_of(model_id)
    with _provider_locks[provider]:
        try:
            llm = get_llm(model_id, read_timeout=HARD_LIMIT).bind_tools([get_time])
            resp = llm.invoke("Gunakan tool get_time untuk kota Jakarta.")
            outcome = bool(getattr(resp, "tool_calls", None))
        except Exception:  # noqa: BLE001
            outcome = "error"
        time.sleep(PROVIDER_GAP.get(provider, DEFAULT_GAP))
    return model_id, outcome


# ---------------------------------------------------------------- state & laporan
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"meta": {}, "models": {}}


def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)  # simpan secara atomik


def names_from_report(path: str) -> set:
    names = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith("- "):
                    toks = s[2:].split()
                    tok = toks[0].rstrip(":") if toks else ""
                    if "/" in tok:
                        names.add(tok)
    return names


def write_report(state: dict) -> None:
    models = state["models"]
    by_status = defaultdict(list)
    for m, r in models.items():
        by_status[r["status"]].append(m)

    lines = [f"LAPORAN UJI MODEL  ({now()})",
             f"Total tercatat: {len(models)}  | batas gagal: {int(HARD_LIMIT)} dtk  | "
             f"FAST <= {int(FAST_MAX)} dtk, NORMAL <= {int(NORMAL_MAX)} dtk, SLOW di atasnya", ""]

    ok = by_status.get("OK", [])
    lines.append(f"OK ({len(ok)}):")
    for tier in ("FAST", "NORMAL", "SLOW"):
        group = sorted(m for m in ok if models[m].get("tier") == tier)
        if group:
            lines.append(f"  -- {tier} ({len(group)}) --")
        for m in group:
            r = models[m]
            extra = ""
            if r.get("tools") is True:
                extra += "  tools=ya"
            elif r.get("tools") is False:
                extra += "  tools=TIDAK"
            elif r.get("tools") == "error":
                extra += "  tools=error"
            hist = r.get("history", [])
            if len(hist) >= 2 and all(h == "OK" for h in hist):
                extra += f"  stabil({len(hist)}x OK)"
            lines.append(f"- {m}  [{r.get('tier')} {r.get('latency')}s]{extra}")

    for title, key in (("EMPTY (balasan kosong)", "EMPTY"),
                       ("TIMEOUT (lebih dari batas waktu)", "TIMEOUT"),
                       ("TRANSIENT (sementara, uji lagi nanti)", "TRANSIENT"),
                       ("THROTTLED_SKIP (dilewati karena rate limit, uji lagi nanti)", "THROTTLED_SKIP")):
        group = sorted(by_status.get(key, []))
        lines += ["", f"{title} ({len(group)}):"] + [f"- {m}" for m in group]

    blocked = by_status.get("BLOCKED", [])
    lines += ["", f"BLOCKED (permanen / butuh tindakan) ({len(blocked)}):"]
    by_reason = defaultdict(list)
    for m in blocked:
        by_reason[models[m].get("reason")].append(m)
    for reason, group in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"  -- {reason} ({len(group)}) --")
        lines += [f"- {m}" for m in sorted(group)]

    vanished = state["meta"].get("vanished", [])
    if vanished:
        lines += ["", f"HILANG DARI DAFTAR MODEL ({len(vanished)}): pernah ada, sekarang tidak ada di all_models.txt"]
        lines += [f"- {m}" for m in vanished]

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def print_summary(state: dict) -> None:
    per = defaultdict(lambda: defaultdict(int))
    for m, r in state["models"].items():
        per[provider_of(m)][r["status"]] += 1
    print("\n=== RINGKASAN PER PROVIDER ===")
    print(f"{'provider':<12}{'OK':>5}{'BLOCKED':>9}{'TRANSIENT':>11}{'EMPTY':>7}{'TIMEOUT':>9}{'SKIP':>6}")
    for p in sorted(per):
        s = per[p]
        print(f"{p:<12}{s['OK']:>5}{s['BLOCKED']:>9}{s['TRANSIENT']:>11}{s['EMPTY']:>7}"
              f"{s['TIMEOUT']:>9}{s['THROTTLED_SKIP']:>6}")


# ---------------------------------------------------------------- alur utama
def interleave(models: list) -> list:
    """Selang-seling antar provider supaya satu provider tidak dibombardir beruntun."""
    queues = defaultdict(list)
    for m in models:
        queues[provider_of(m)].append(m)
    order, pools = [], list(queues.values())
    while any(pools):
        for q in pools:
            if q:
                order.append(q.pop(0))
    return order


def run_pool(items, fn, workers, on_result):
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [ex.submit(fn, it) for it in items]
        for fut in as_completed(futures):
            on_result(*fut.result())
    except KeyboardInterrupt:
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    ex.shutdown()


def main():
    ap = argparse.ArgumentParser(description="Uji ketersediaan dan kapabilitas model OmniRoute")
    ap.add_argument("--all", action="store_true", help="uji ulang semua model (bukan hanya yang belum/transient)")
    ap.add_argument("--only", default="", help="provider yang diuji, pisah koma (contoh: kr,ag)")
    ap.add_argument("--skip", default="", help="provider yang dilewati, pisah koma")
    ap.add_argument("--tools", action="store_true", help="uji kemampuan tool-calling untuk model OK")
    ap.add_argument("--workers", type=int, default=8, help="jumlah worker paralel (default: 8)")
    args = ap.parse_args()
    only = {p for p in args.only.split(",") if p}
    skip = {p for p in args.skip.split(",") if p}

    try:
        with open(LIST_FILE, encoding="utf-8") as f:
            listed = [ln.strip() for ln in f if ln.strip()]
    except FileNotFoundError:
        raise SystemExit("all_models.txt tidak ditemukan. Jalankan dulu: python list_models.py")
    current = [m for m in listed if m not in KNOWN_COMBOS]

    state = load_state()
    meta = state["meta"]
    prev_seen = set(meta.get("seen", [])) or names_from_report(REPORT_FILE)
    gone = (prev_seen - set(current)) - KNOWN_COMBOS
    vanished = (set(meta.get("vanished", [])) | gone) - set(current)
    meta["vanished"] = sorted(vanished)
    meta["seen"] = sorted(current)
    if vanished:
        print(f"PERHATIAN: {len(vanished)} model pernah ada tapi sekarang TIDAK ada di {LIST_FILE} "
              f"(contoh: {', '.join(sorted(vanished)[:5])}). Bukan gagal tes, tapi hilang dari daftar OmniRoute.\n")

    models_state = state["models"]
    todo = []
    for m in current:
        p = provider_of(m)
        if (only and p not in only) or p in skip:
            continue
        prev = models_state.get(m)
        if args.all or prev is None or prev["status"] in RETRY_STATUSES:
            todo.append(m)
    todo = interleave(todo)

    print(f"Model di daftar: {len(current)} | sudah tercatat: {len(models_state)} | "
          f"akan diuji sekarang: {len(todo)} ({args.workers} paralel, 1 per provider)")
    print(f"Batas waktu: FAST <= {int(FAST_MAX)} dtk, NORMAL <= {int(NORMAL_MAX)} dtk, "
          f"SLOW di atasnya, > {int(HARD_LIMIT)} dtk = TIMEOUT\n")

    counter = {"n": 0}

    def on_probe(model_id, result):
        counter["n"] += 1
        with _state_lock:
            prev = models_state.get(model_id, {})
            hist = (prev.get("history", []) + [result["status"]])[-3:]
            entry = {**result, "history": hist, "tested_at": now()}
            if result["status"] == "OK" and prev.get("tools") is not None:
                entry["tools"] = prev["tools"]
            models_state[model_id] = entry
            save_state(state)
        info = f"{result.get('tier')} {result.get('latency')}s" if result["status"] == "OK" \
            else (result.get("reason") or "")
        print(f"[{counter['n']}/{len(todo)}] {model_id:<52} {result['status']:<14} {info}")

    def on_tools(model_id, outcome):
        with _state_lock:
            models_state[model_id]["tools"] = outcome
            save_state(state)
        print(f"[tools] {model_id:<52} {'ya' if outcome is True else outcome if outcome == 'error' else 'TIDAK'}")

    interrupted = False
    try:
        if todo:
            run_pool(todo, probe_worker, args.workers, on_probe)
        if args.tools:
            targets = [m for m, r in models_state.items()
                       if r["status"] == "OK" and r.get("tools") is None
                       and (not only or provider_of(m) in only) and provider_of(m) not in skip]
            targets = interleave(targets)
            print(f"\nUji tool-calling untuk {len(targets)} model OK ...")
            run_pool(targets, tools_worker, args.workers, on_tools)
    except KeyboardInterrupt:
        interrupted = True
        print("\nDihentikan manual. Progres sudah tersimpan; jalankan lagi untuk melanjutkan.")
    finally:
        with _state_lock:
            save_state(state)
            write_report(state)

    print_summary(state)
    ok = sum(1 for r in models_state.values() if r["status"] == "OK")
    print(f"\nTotal OK: {ok}/{len(models_state)}. Laporan: {REPORT_FILE} | data mesin: {STATE_FILE}")
    if interrupted:
        os._exit(130)  # jangan menunggu thread yang masih menunggu jawaban provider


if __name__ == "__main__":
    main()
