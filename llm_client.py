"""OmniRoute OpenAI-compatible model client."""

import os
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv()

BASE_URL = os.getenv("OMNIROUTE_BASE_URL", "http://localhost:20128/v1").rstrip("/")
API_KEY = os.getenv("OMNIROUTE_API_KEY")


def get_llm(model_id: str, temperature: float = 0, read_timeout: float = 120.0) -> ChatOpenAI:
    """Create a LangChain ChatOpenAI client routed through OmniRoute."""
    if not API_KEY:
        raise RuntimeError("OMNIROUTE_API_KEY belum terisi di .env")

    return ChatOpenAI(
        base_url=BASE_URL,
        api_key=API_KEY,
        model=model_id,
        temperature=temperature,
        timeout=httpx.Timeout(connect=10.0, read=read_timeout, write=10.0, pool=10.0),
        max_retries=0,
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
