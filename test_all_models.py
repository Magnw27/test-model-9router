"""Termux-first OmniRoute model benchmark. Uses only requests + python-dotenv."""
from __future__ import annotations
import argparse, json, os, re, threading, time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
import requests
from llm_client import chat_completion

LIST_FILE="all_models.txt"; STATE_FILE="test_state.json"; REPORT_FILE="test_results.txt"
PROMPT="Balas hanya dengan satu kata: OK"
FAST_MAX=15.0; NORMAL_MAX=45.0; HARD_LIMIT=120.0
DEFAULT_WORKERS=2; MAX_WORKERS=8; MAX_ATTEMPTS=3
BACKOFF=(5.0,15.0,30.0); RETRY_CAP=45.0; BREAKER_LIMIT=8; DEFAULT_GAP=1.0
RETRY_STATUSES={"TRANSIENT","THROTTLED_SKIP"}; KNOWN_COMBOS=set()
BLOCK_RULES=[
 ("invalid_model",("invalid_model_id","model_not_supported","model_not_found","does not exist or you do not have access","please check the model","[404]","not found for account","unknown model")),
 ("retired",("retired","end of life",'"gone"',"deprecated")),
 ("unavailable",("every channel behind it is switched off",)),
 ("no_credit",("insufficient_credits","insufficient balance","credits required","requires an active subscription","not included in your free usage")),
 ("incompatible",("unexpected response type","only available on agentic harnesses","decisions model","cannot be used with the chat/completions")),
 ("auth",("no active credentials","api key format is incorrect","unauthorized","[401]","[403]","http 403")),
]
TRANSIENT_HINTS=("429","too many requests","rate limit","rate-limited","high demand","temporarily","overloaded","resourceexhausted","[500]","[502]","[503]","internal error","service unavailable","gateway timeout")
_state_lock=threading.Lock(); _breaker_lock=threading.Lock()
_provider_locks=defaultdict(threading.Lock); _breaker=defaultdict(int)

def now(): return __import__("datetime").datetime.now().isoformat(timespec="seconds")
def provider_of(model_id): return model_id.split("/",1)[0]
def text_of(content):
    if isinstance(content,str): return content.strip()
    if isinstance(content,list):
        return "".join(p if isinstance(p,str) else str(p.get("text") or "") for p in content if isinstance(p,(str,dict))).strip()
    return str(content or "").strip()

def parse_wait(msg):
    m=re.search(r"reset after\s+(?:(\d+)m)?\s*(?:(\d+)s)?",msg,re.I)
    return None if not m else int(m.group(1) or 0)*60+int(m.group(2) or 0)

def classify(exc,msg):
    low=msg.lower()
    if isinstance(exc,(requests.Timeout,TimeoutError)) or "timed out" in low or "timeout" in low: return "TIMEOUT","melewati batas waktu"
    for reason,needles in BLOCK_RULES:
        if any(n in low for n in needles): return "BLOCKED",reason
    if any(n in low for n in TRANSIENT_HINTS): return "TRANSIENT",None
    return "TRANSIENT","unknown"

def tier_of(latency): return "FAST" if latency<=FAST_MAX else ("NORMAL" if latency<=NORMAL_MAX else "SLOW")

def run_probe(model_id):
    for attempt in range(1,MAX_ATTEMPTS+1):
        started=time.monotonic()
        try:
            data=chat_completion(model_id,[{"role":"user","content":PROMPT}],timeout=HARD_LIMIT)
            latency=time.monotonic()-started
            choices=data.get("choices") or []; message=(choices[0].get("message",{}) if choices else {})
            text=text_of(message.get("content",""))
            if not text and not message.get("tool_calls"):
                return {"status":"EMPTY","reason":"balasan kosong","latency":round(latency,1),"attempts":attempt}
            return {"status":"OK","tier":tier_of(latency),"latency":round(latency,1),"detail":text[:60],"attempts":attempt}
        except Exception as exc:
            msg=str(exc); kind,reason=classify(exc,msg); latency=round(time.monotonic()-started,1)
            if kind=="TRANSIENT" and attempt<MAX_ATTEMPTS:
                time.sleep(min(parse_wait(msg) or BACKOFF[attempt-1],RETRY_CAP)); continue
            return {"status":kind,"reason":reason,"detail":msg[:300],"latency":latency,"attempts":attempt}
    return {"status":"TRANSIENT","reason":"unknown","attempts":MAX_ATTEMPTS}

def probe_worker(model_id):
    provider=provider_of(model_id)
    with _provider_locks[provider]:
        with _breaker_lock: tripped=_breaker[provider]>=BREAKER_LIMIT
        if tripped: result={"status":"THROTTLED_SKIP","reason":f"provider {provider} terlalu sering rate-limit"}
        else:
            result=run_probe(model_id); time.sleep(DEFAULT_GAP)
            with _breaker_lock:
                _breaker[provider]=_breaker[provider]+1 if result["status"]=="TRANSIENT" else 0
    return model_id,result

TOOL_SCHEMA=[{"type":"function","function":{"name":"get_time","description":"Return the current time for a city.","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"],"additionalProperties":False}}}]

def tools_worker(model_id):
    provider=provider_of(model_id)
    with _provider_locks[provider]:
        try:
            data=chat_completion(model_id,[{"role":"user","content":"Gunakan tool get_time untuk kota Jakarta."}],timeout=HARD_LIMIT,tools=TOOL_SCHEMA)
            message=((data.get("choices") or [{}])[0]).get("message",{})
            outcome=bool(message.get("tool_calls"))
        except Exception as exc:
            print(f"[tools] {model_id}: {str(exc)[:160]}"); outcome="error"
        time.sleep(DEFAULT_GAP)
    return model_id,outcome

def load_state():
    if not os.path.exists(STATE_FILE): return {"meta":{},"models":{}}
    try:
        with open(STATE_FILE,encoding="utf-8") as f: data=json.load(f)
        if not isinstance(data,dict): raise ValueError
        data.setdefault("meta",{}); data.setdefault("models",{}); return data
    except (OSError,ValueError,json.JSONDecodeError):
        print("Peringatan: state rusak; memulai state baru."); return {"meta":{},"models":{}}

def save_state(state):
    tmp=STATE_FILE+".tmp"
    with open(tmp,"w",encoding="utf-8") as f: json.dump(state,f,ensure_ascii=False,indent=2)
    os.replace(tmp,STATE_FILE)

def write_report(state):
    groups=defaultdict(list)
    for model,result in state["models"].items(): groups[result.get("status","UNKNOWN")].append(model)
    lines=[f"OMNIROUTE MODEL BENCHMARK — {now()}",f"Total: {len(state['models'])}",f"FAST <= {FAST_MAX:g}s | NORMAL <= {NORMAL_MAX:g}s | SLOW > {NORMAL_MAX:g}s | TIMEOUT > {HARD_LIMIT:g}s",""]
    for status in ("OK","EMPTY","TIMEOUT","TRANSIENT","THROTTLED_SKIP","BLOCKED"):
        items=sorted(groups.get(status,[])); lines.append(f"{status} ({len(items)})")
        for model in items:
            r=state["models"][model]
            if status=="OK":
                tools=r.get("tools"); suffix="" if tools is None else f" | tools={'YES' if tools is True else 'NO' if tools is False else 'ERROR'}"
                lines.append(f"- {model} | {r.get('tier')} | {r.get('latency')}s{suffix}")
            else: lines.append(f"- {model} | {r.get('reason') or r.get('detail') or '-'}")
        lines.append("")
    vanished=state["meta"].get("vanished",[])
    if vanished: lines += [f"HILANG DARI DAFTAR ({len(vanished)})"]+[f"- {m}" for m in vanished]+[""]
    with open(REPORT_FILE,"w",encoding="utf-8") as f: f.write("\n".join(lines))

def print_summary(state):
    counts=defaultdict(int)
    for r in state["models"].values(): counts[r.get("status","UNKNOWN")]+=1
    print("\n=== RINGKASAN ===")
    for s in ("OK","BLOCKED","TRANSIENT","THROTTLED_SKIP","EMPTY","TIMEOUT"): print(f"{s:<16} {counts[s]}")

def interleave(models):
    queues=defaultdict(list)
    for m in models: queues[provider_of(m)].append(m)
    out=[]
    while any(queues.values()):
        for q in queues.values():
            if q: out.append(q.pop(0))
    return out

def run_pool(items,fn,workers,on_result):
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures=[executor.submit(fn,item) for item in items]
        for future in as_completed(futures): on_result(*future.result())

def main():
    ap=argparse.ArgumentParser(description="OmniRoute benchmark — Termux optimized")
    ap.add_argument("--all",action="store_true"); ap.add_argument("--only",default=""); ap.add_argument("--skip",default="")
    ap.add_argument("--tools",action="store_true"); ap.add_argument("--workers",type=int,default=DEFAULT_WORKERS)
    args=ap.parse_args()
    if not 1<=args.workers<=MAX_WORKERS: ap.error(f"--workers harus 1-{MAX_WORKERS}")
    only={x.strip() for x in args.only.split(",") if x.strip()}; skip={x.strip() for x in args.skip.split(",") if x.strip()}
    try:
        with open(LIST_FILE,encoding="utf-8") as f: current=[x.strip() for x in f if x.strip() and x.strip() not in KNOWN_COMBOS]
    except FileNotFoundError: raise SystemExit("all_models.txt tidak ditemukan. Jalankan: python list_models.py")
    state=load_state(); models_state=state["models"]; previous=set(state["meta"].get("seen",[]))
    state["meta"]["vanished"]=sorted(previous-set(current)); state["meta"]["seen"]=sorted(current)
    todo=[]
    for model in current:
        provider=provider_of(model); prev=models_state.get(model)
        if (only and provider not in only) or provider in skip: continue
        if args.all or prev is None or prev.get("status") in RETRY_STATUSES: todo.append(model)
    todo=interleave(todo)
    print(f"Model: {len(current)} | state: {len(models_state)} | akan diuji: {len(todo)} | workers: {args.workers}")
    counter=0
    def on_probe(model,result):
        nonlocal counter
        counter+=1
        with _state_lock:
            old=models_state.get(model,{})
            models_state[model]={**result,"history":(old.get("history",[])+[result["status"]])[-5:],"tested_at":now()}
            if "tools" in old and result["status"]=="OK": models_state[model]["tools"]=old["tools"]
            save_state(state)
        detail=f"{result.get('tier')} {result.get('latency')}s" if result["status"]=="OK" else result.get("reason") or result.get("detail") or ""
        print(f"[{counter}/{len(todo)}] {model} -> {result['status']} {detail}")
    def on_tools(model,outcome):
        with _state_lock: models_state[model]["tools"]=outcome; save_state(state)
        print(f"[tools] {model} -> {outcome}")
    try:
        if todo: run_pool(todo,probe_worker,args.workers,on_probe)
        if args.tools:
            targets=[m for m,r in models_state.items() if r.get("status")=="OK" and r.get("tools") is None and (not only or provider_of(m) in only) and provider_of(m) not in skip]
            if targets: print(f"Tool calling: {len(targets)} model"); run_pool(interleave(targets),tools_worker,args.workers,on_tools)
    except KeyboardInterrupt:
        print("\nDihentikan. State sudah disimpan; jalankan lagi untuk melanjutkan.")
    finally:
        with _state_lock: save_state(state); write_report(state)
    print_summary(state); print(f"\nReport: {REPORT_FILE}\nState : {STATE_FILE}")
    return 130 if False else 0

if __name__=="__main__": raise SystemExit(main())
