"""Active LLM backend: any OpenAI-compatible API (local llama.cpp,
LM Studio, Ollama, OpenRouter, Together, Groq, DeepSeek...).

- get_active() -> (base_url, api_key, model), env-driven, localhost default.
- list_models() fetches /v1/models (OpenAI {"data":[{"id"}]} and llama
  {"models":[{"model"}]} shapes).
- set_active() persists to yymbot/.env + live os.environ (no restart).
Model switching is global (one backend for all users) -> admin commands.
"""
import json
import logging
import os

logger: logging.Logger = logging.getLogger(__name__)

ENV_BASE = "LLM_BASE_URL"
ENV_KEY = "LLM_API_KEY"
ENV_MODEL = "LLM_MODEL"
DEFAULT_BASE = "http://localhost:8080/v1"
NOKEY = "sk-noop"

_PENDING: dict = {}


def get_extra():
    """Second API fallback (server-only secrets, never committed).

    Returns (base, key, model) or None when EXTRA_BASE_URL is unset.
    """
    base = (os.getenv("EXTRA_BASE_URL", "") or "").rstrip("/")
    if not base:
        return None
    key = os.getenv("EXTRA_API_KEY", "") or NOKEY
    model = os.getenv("EXTRA_MODEL", "") or "extra"
    return base, key, model


def model_size_b(mid: str):
    """Guess params in billions from model id (30b->30, 8x22b->176)."""
    import re as _re
    s = (mid or "").lower()
    m = _re.search(r"(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*b", s)
    if m:
        try:
            return float(m.group(1)) * float(m.group(2))
        except Exception:
            pass
    m = _re.search(r"(\d+(?:\.\d+)?)\s*b(?!its?)", s)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            pass
    return None


def allowed_by_cap(mid: str) -> bool:
    """MODEL_MAXB env caps model class (0/unset = no cap, forks decide)."""
    try:
        cap = float(os.getenv("MODEL_MAXB", "") or 0)
    except Exception:
        return True
    if not cap:
        return True
    size = model_size_b(mid or "")
    if size is None:
        return True
    return size <= cap


def get_active():
    """(base_url, api_key, model). Never raises, always usable defaults."""
    base = (os.getenv(ENV_BASE, "") or os.getenv("LLAMA_BASE_URL", "")
            or DEFAULT_BASE)
    key = os.getenv(ENV_KEY, "") or NOKEY
    model = os.getenv(ENV_MODEL, "") or "local"
    return base.rstrip("/"), key, model


def _headers(api_key: str) -> dict:
    h = {"Content-Type": "application/json"}
    if api_key and api_key != NOKEY:
        h["Authorization"] = "Bearer " + api_key
    return h


def list_models(base: str = "", api_key: str = "",
                timeout: int = 12) -> list:
    """Fetch model ids. Raises RuntimeError with short reason on failure."""
    import urllib.request
    b, k, _ = get_active()
    base = (base or b).rstrip("/")
    api_key = api_key if api_key != "" else k
    url = base + "/models"
    try:
        req = urllib.request.Request(url, headers=_headers(api_key))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if getattr(r, "status", 200) != 200:
                raise RuntimeError(f"HTTP {getattr(r, 'status', '?')}")
            d = json.loads(r.read(200000).decode("utf-8", "replace"))
    except Exception as e:
        raise RuntimeError(f"unreachable: {type(e).__name__} {e}"[:160])
    out = []
    items = d.get("data") or d.get("models") or []
    for m in items:
        if not isinstance(m, dict):
            continue
        mid = (m.get("id") or m.get("model") or m.get("name") or "")
        if isinstance(mid, str) and mid.strip():
            # llama returns full gguf paths — keep short name too
            short = mid.strip().split("/")[-1]
            out.append(short if len(short) < 80 else mid.strip()[:80])
    seen, ids = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            ids.append(x)
    if not ids:
        raise RuntimeError("empty model list")
    return ids


def set_pending(uid, models: list) -> None:
    try:
        _PENDING[int(uid)] = list(models)[:100]
    except Exception:
        pass


def pop_pending(uid):
    try:
        return _PENDING.pop(int(uid), [])
    except Exception:
        return []


def persist(base: str, api_key: str, model: str) -> None:
    """Write backend to yymbot/.env + live environ (no restart needed).

    Empty args keep the current value (model-only switch safe).
    """
    cb, ck, cm = get_active()
    base = (base or "").strip() or cb
    if api_key is None or api_key == "":
        api_key = ck
    model = (model or "").strip() or cm
    base = base.rstrip("/") or DEFAULT_BASE
    try:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        envp = os.path.join(here, "..", "..", "yymbot", ".env")
        envp = os.path.normpath(envp)
        vals = {ENV_BASE: base, ENV_KEY: api_key, ENV_MODEL: model,
                "BASE_URL": base + "/chat/completions",
                "API_KEY": api_key, "MODEL": model}
        lines = []
        try:
            with open(envp, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            pass
        seen = set()
        for i, ln in enumerate(lines):
            k = ln.split("=", 1)[0].strip()
            if k in vals:
                lines[i] = k + "=" + vals[k]
                seen.add(k)
        for k, v in vals.items():
            if k not in seen:
                lines.append(k + "=" + v)
        with open(envp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        logger.warning("persist backend failed: %s", e)
    for k, v in ((ENV_BASE, base), (ENV_KEY, api_key), (ENV_MODEL, model),
                 ("LLAMA_BASE_URL", base), ("BASE_URL",
                                            base + "/chat/completions"),
                 ("MODEL", model), ("API_KEY", api_key)):
        try:
            os.environ[k] = v
        except Exception:
            pass
    logger.info("backend active: %s model=%s", base, model)
