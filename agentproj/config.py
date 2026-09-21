from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()

# --- Required ---
TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
BOT_USERNAME: str = os.environ["BOT_USERNAME"].lstrip("@").lower()

# --- LLM Backend ---
LLM_BACKEND: str = os.getenv("LLM_BACKEND", "ollama").lower()  # legacy: "ollama" | "gemini"
LLM_MODEL: str = os.getenv("LLM_MODEL", "gemini-2.0-flash")  # legacy Gemini fallback model

# --- Gemini ---
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GEMINI_FALLBACK_MODEL: str = os.getenv("GEMINI_FALLBACK_MODEL", LLM_MODEL)

# --- Ollama ---
OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "gemma4:26b")
OLLAMA_NUM_CTX: int = int(os.getenv("OLLAMA_NUM_CTX", "65536"))
OLLAMA_THINK: bool = os.getenv("OLLAMA_THINK", "false").lower() in {"1", "true", "yes"}

# --- Admin ---
_raw_admins = os.getenv("ADMIN_USER_IDS", "")
ADMIN_USER_IDS: set[int] = {
    int(x.strip()) for x in _raw_admins.split(",") if x.strip().isdigit()
}

# --- Runtime mutable backend ---
# Initialised in bot.py on startup; replaced by /model command.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from backends.base import LLMBackend

_active_backend: "LLMBackend | None" = None


def get_backend() -> "LLMBackend":
    if _active_backend is None:
        raise RuntimeError("LLM backend not initialised — call set_active_backend() first")
    return _active_backend


def set_active_backend(model_spec: str) -> str:
    """
    Set the active backend from a model spec string.

    Accepted formats:
      "ollama:llama3.2"       → OllamaBackend with model "llama3.2"
      "gemini-2.0-flash"      → GeminiBackend with that model name
      "gemini:gemini-2.0-flash" → same as above (explicit prefix)

    Returns a human-readable description of what was set.
    """
    global _active_backend
    from backends.gemini import GeminiBackend
    from backends.ollama import OllamaBackend

    if model_spec.startswith("ollama:"):
        model_name = model_spec[len("ollama:"):]
        _active_backend = OllamaBackend(
            model_name=model_name,
            host=OLLAMA_HOST,
            num_ctx=OLLAMA_NUM_CTX,
            think=OLLAMA_THINK,
        )
        return f"ollama:{model_name}"
    elif model_spec.startswith("gemini:"):
        model_name = model_spec[len("gemini:"):]
        _active_backend = GeminiBackend(model_name=model_name, api_key=GEMINI_API_KEY)
        return f"gemini:{model_name}"
    else:
        # Default: treat as Gemini model name
        _active_backend = GeminiBackend(model_name=model_spec, api_key=GEMINI_API_KEY)
        return f"gemini:{model_spec}"


def current_model_name() -> str:
    if _active_backend is None:
        return "(none)"
    return _active_backend.name


def using_ollama() -> bool:
    return current_model_name().startswith("ollama:")


def set_gemini_fallback_backend() -> str | None:
    if not GEMINI_API_KEY:
        return None
    return set_active_backend(f"gemini:{GEMINI_FALLBACK_MODEL}")
