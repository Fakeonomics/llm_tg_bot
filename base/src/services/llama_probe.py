"""Probe the local llama.cpp server (port 8080, OpenAI-compatible /v1/models).

Returns which model is running and whether it has vision (image input).
"""
import json
import logging
import urllib.request

logger: logging.Logger = logging.getLogger(__name__)

VISION_CAPS = {"image", "vision", "multimodal", "image-input", "images"}
VISION_NAME_HINTS = (
    "vl", "vision", "llava", "minicpm-v", "qwen2-vl", "qwen-vl",
    "clip", "siglip", "mmproj", "pixtral", "idefics",
)


def _model_has_vision(model: dict) -> bool:
    caps = {str(c).lower() for c in (model.get("capabilities") or [])}
    if caps & VISION_CAPS:
        return True
    name = str(model.get("name") or model.get("model") or "").lower()
    return any(h in name for h in VISION_NAME_HINTS)


def probe_llama(base_url: str = "http://localhost:8080/v1") -> dict:
    """Query /v1/models. Never raises; returns a status dict."""
    out = {
        "ok": False, "models": [], "active_model_id": "",
        "has_vision": False, "error": "",
    }
    url = base_url.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.load(r)
    except Exception as e:  # server down / unreachable
        out["error"] = str(e)
        logger.warning("llama probe failed: %s", e)
        return out
    models = data.get("models") or data.get("data") or []
    for m in models:
        mid = str(m.get("model") or m.get("name") or m.get("id") or "")
        caps = [str(c) for c in (m.get("capabilities") or [])]
        out["models"].append({"id": mid, "capabilities": caps})
    if out["models"]:
        out["ok"] = True
        out["active_model_id"] = out["models"][0]["id"]
        out["has_vision"] = any(
            _model_has_vision(m) for m in models
        )
    return out


THINK_HINTS = ("r1", "distill", "qwq", "reason", "think", "ornith",
               "tiel", "cyber")


def server_props(base_url: str = "http://localhost:8080") -> dict:
    """llama.cpp /props: n_ctx, params. Never raises."""
    out = {"ok": False, "n_ctx": 0, "model_path": ""}
    try:
        with urllib.request.urlopen(
                base_url.rstrip("/") + "/props", timeout=5) as r:
            d = json.load(r)
    except Exception:
        return out
    try:
        out["n_ctx"] = int(d.get("total_slots", [{}])[0].get("n_ctx", 0)
                           or d.get("default_generation_settings", {}).get("n_ctx", 0))
    except Exception:
        pass
    if not out["n_ctx"]:
        try:
            out["n_ctx"] = int(d.get("n_ctx", 0))
        except Exception:
            pass
    out["model_path"] = str(d.get("model_path", ""))
    out["ok"] = True
    return out


def describe() -> dict:
    """Sanitized model card for display: short name, vision, ctx, thinking.

    Basename only (no dirs/users/hosts). Works for ANY model on the port
    (hot-swap safe, probed live, no restart needed).
    """
    import os as _os
    p = probe_llama()
    if not p.get("ok"):
        return {"ok": False}
    mid = p["active_model_id"]
    base = _os.path.basename(mid)
    low = base.lower()
    sp = server_props()
    return {"ok": True,
            "name": base or "model",
            "vision": has_vision(),
            "n_ctx": sp.get("n_ctx", 0),
            "thinking": any(h in low for h in THINK_HINTS)}


_VISION_CACHE: dict = {}


def has_vision() -> bool:
    """Does the LOADED model see images? Caps/name fast path, else one tiny
    empirical vision call (1px image). Cached per model id (hot-swap safe)."""
    try:
        p = probe_llama()
        if not p.get("ok"):
            return False
        mid = p.get("active_model_id", "")
        if mid in _VISION_CACHE:
            return _VISION_CACHE[mid]
        if p.get("has_vision"):
            _VISION_CACHE[mid] = True
            return True
        tiny_jpg = (
            "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsUF"
            "AQDBQEA//2Q==")
        body = json.dumps({
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "ok?"},
                {"type": "image_url", "image_url": {
                    "url": "data:image/jpeg;base64," + tiny_jpg}}]}],
            "max_tokens": 1}).encode()
        req = urllib.request.Request(
            "http://localhost:8080/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                ok = r.status == 200
        except Exception:
            ok = False
        _VISION_CACHE[mid] = ok
        return ok
    except Exception:
        return False
