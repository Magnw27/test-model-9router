"""Lightweight OpenAI-compatible HTTP client for OmniRoute."""
from __future__ import annotations
import os
from datetime import datetime, timezone
from typing import Any
import requests
from dotenv import load_dotenv

load_dotenv()
BASE_URL = os.getenv("OMNIROUTE_BASE_URL", "http://localhost:20128/v1").rstrip("/")
API_KEY = os.getenv("OMNIROUTE_API_KEY", "").strip()

class OmniRouteError(RuntimeError):
    pass

def _headers() -> dict[str, str]:
    if not API_KEY:
        raise OmniRouteError("OMNIROUTE_API_KEY belum diatur di .env")
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json", "Accept": "application/json"}

def _request(method: str, path: str, *, timeout: float = 30.0, json: dict[str, Any] | None = None) -> requests.Response:
    url = f"{BASE_URL}/{path.lstrip('/')}"
    try:
        response = requests.request(method, url, headers=_headers(), json=json, timeout=(10.0, timeout))
    except requests.Timeout as exc:
        raise OmniRouteError(f"Request timeout setelah {timeout:.0f}s: {url}") from exc
    except requests.RequestException as exc:
        raise OmniRouteError(f"Gagal terhubung ke OmniRoute: {exc}") from exc
    if not response.ok:
        detail = response.text.strip().replace("\n", " ")[:1000]
        raise OmniRouteError(f"HTTP {response.status_code} dari {url}: {detail}")
    return response

def list_models(*, timeout: float = 30.0) -> list[dict[str, Any]]:
    data = _request("GET", "/models", timeout=timeout).json()
    return [item for item in data.get("data", []) if item.get("id")]

def chat_completion(model: str, messages: list[dict[str, Any]], *, timeout: float = 120.0, temperature: float = 0.0, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return _request("POST", "/chat/completions", timeout=timeout, json=payload).json()

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
