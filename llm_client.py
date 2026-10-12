"""Lightweight OpenAI-compatible client for OmniRoute.

Uses requests directly instead of LangChain/OpenAI SDK so it installs cleanly
on Termux and does not need compiled dependencies such as jiter.
"""
import os
import threading
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = (os.getenv("OMNIROUTE_BASE_URL") or os.getenv("NINEROUTER_BASE_URL", "")).strip().rstrip("/")
API_KEY = (os.getenv("OMNIROUTE_API_KEY") or os.getenv("NINEROUTER_API_KEY", "")).strip()
_sessions = threading.local()


class ModelTimeoutError(TimeoutError):
    """A model request hit its connection/read timeout."""


class APIError(RuntimeError):
    """HTTP error returned by an OpenAI-compatible endpoint."""

    def __init__(self, status_code: int, message: str, headers=None):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.message = message
        super().__init__(f"HTTP {status_code}: {message}")


class APITransportError(RuntimeError):
    """Network failure before a useful API response was received."""


class ModelResponse:
    def __init__(self, content: Any = "", tool_calls: Optional[List[dict]] = None):
        self.content = content if content is not None else ""
        self.tool_calls = tool_calls or []


def validate_config() -> None:
    if not BASE_URL or not API_KEY or API_KEY.lower() in (
        "isi_api_key_9router_di_sini", "api_key_milikmu", "enter_your_9router_api_key_here", "isi_api_key_omniroute_di_sini"
    ):
        raise RuntimeError(
            "Konfigurasi belum lengkap. Isi NINEROUTER_BASE_URL dan "
            "NINEROUTER_API_KEY yang valid di file .env terlebih dahulu."
        )
    if not BASE_URL.startswith(("http://", "https://")):
        raise RuntimeError("NINEROUTER_BASE_URL harus diawali http:// atau https://")


def _thread_session():
    """Reuse HTTP connections per worker thread without sharing Session across threads."""
    session = getattr(_sessions, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "test-model-9router/2.1",
        })
        _sessions.session = session
    return session


class ChatClient:
    """Small, dependency-light wrapper for /chat/completions."""

    def __init__(self, model: str, temperature: float = 0, read_timeout: float = 120.0):
        validate_config()
        self.model = model
        self.temperature = temperature
        self.read_timeout = max(1.0, float(read_timeout))

    def invoke(self, prompt: str, tools: Optional[List[dict]] = None) -> ModelResponse:
        url = f"{BASE_URL}/chat/completions"
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            response = _thread_session().post(
                url,
                headers={"Authorization": f"Bearer {API_KEY}"},
                json=payload,
                timeout=(10.0, self.read_timeout),
            )
        except requests.exceptions.Timeout as exc:
            raise ModelTimeoutError(str(exc) or "Request timed out") from exc
        except requests.exceptions.RequestException as exc:
            raise APITransportError(str(exc)) from exc

        if not response.ok:
            try:
                body = response.json()
                error = body.get("error", body) if isinstance(body, dict) else body
                if isinstance(error, dict):
                    message = error.get("message") or error.get("detail") or str(error)
                else:
                    message = str(error)
            except (ValueError, TypeError):
                message = response.text or "empty error response"
            raise APIError(response.status_code, str(message)[:1000], response.headers)

        try:
            data = response.json()
        except ValueError as exc:
            raise APITransportError("Endpoint mengirim JSON yang tidak valid") from exc

        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            return ModelResponse()

        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        if not isinstance(message, dict):
            return ModelResponse()
        return ModelResponse(
            content=message.get("content", ""),
            tool_calls=message.get("tool_calls") or [],
        )


def get_llm(model_id: str, temperature: float = 0, read_timeout: float = 120.0) -> ChatClient:
    """Create a lightweight client for the requested model."""
    return ChatClient(model_id, temperature=temperature, read_timeout=read_timeout)
