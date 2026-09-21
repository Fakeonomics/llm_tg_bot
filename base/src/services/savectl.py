"""Per-user SAVE (dialog memory) control, managed by admin.

save ON (default): context-aware dialog, history kept in RAM (temporary,
dies with restart — nothing persisted to disk).
save OFF: stateless/privacy mode — history is wiped after each answer,
the model sees only the current message (+system prompt).
The JSON stores only on/off flags, never dialog content.
"""
import json
import logging
import os

logger: logging.Logger = logging.getLogger(__name__)

SAVE_PATH = os.getenv(
    "TGBOT_SAVE",
    os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "save.json"),
)


def _load() -> dict:
    try:
        with open(SAVE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (FileNotFoundError, ValueError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    return d


def _save(d: dict) -> None:
    tmp = SAVE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SAVE_PATH)


def is_save_on(user_id: int) -> bool:
    """Default True; admin can switch a user to stateless (False)."""
    return bool(_load().get(str(int(user_id)), True))


def set_save(user_id: int, on: bool) -> dict:
    d = _load()
    d[str(int(user_id))] = bool(on)
    _save(d)
    return d


def list_saves() -> dict:
    return {k: bool(v) for k, v in _load().items()}


def enforce_in_place(chat) -> None:
    """Wipe a Chat's history when save is OFF (keep system prompt)."""
    try:
        if is_save_on(chat.user_id):
            return
        msgs = getattr(chat, "messages", [])
        keep = [msgs[0]] if msgs else []
        chat.messages = keep
        logger.debug("save OFF for %s: history wiped", chat.user_id)
    except Exception as e:
        logger.debug("save enforce failed: %s", e)
