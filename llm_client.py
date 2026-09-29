"""
Helper untuk memanggil model lewat 9Router berdasarkan Model ID.
Menggunakan library LangChain ChatOpenAI yang kompatibel dengan OpenAI API.
"""

import os
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv()

BASE_URL = os.getenv("NINEROUTER_BASE_URL")
API_KEY = os.getenv("NINEROUTER_API_KEY")


def get_llm(model_id: str, temperature: float = 0, read_timeout: float = 120.0) -> ChatOpenAI:
    """Menginisialisasi klien ChatOpenAI untuk model_id tertentu via 9Router.
    read_timeout = batas tunggu jawaban (detik). Default 120 = batas gagal 2 menit.
    """
    if not BASE_URL or not API_KEY:
        raise RuntimeError(
            "NINEROUTER_BASE_URL atau NINEROUTER_API_KEY belum terisi di .env"
        )
    return ChatOpenAI(
        base_url=BASE_URL,
        api_key=API_KEY,
        model=model_id,
        temperature=temperature,
        timeout=httpx.Timeout(connect=10.0, read=read_timeout, write=10.0, pool=10.0),
        max_retries=0,  # retry diatur secara manual oleh skrip penguji
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
