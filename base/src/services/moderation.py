"""Content protection for an uncensored local model.

ON by default for everyone except admin / exempted users.
Admin toggles with /protect and /exempt.
Uses Detoxify (unitaryai/detoxify, 1301★) when installed, otherwise a
deterministic RU/EN keyword blocklist. Never crashes the bot.
"""
import json
import logging
import os
import re

logger: logging.Logger = logging.getLogger(__name__)

FILTER_PATH = os.getenv(
    "TGBOT_FILTER",
    os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "filter.json"),
)

REFUSE_RU = (
    "Не могу помочь с этой темой — она запрещена настройками защиты. "
    "Если считаешь это ошибкой, обратись к администратору."
)

# Forbidden-topic hints (RU/EN). Deterministic first line of defence.
BLOCKED_RE = re.compile(
    r"(изготовлени\w*\s+(наркотик|взрывчат|оружи)|"
    r"рецепт\s+(наркотик|взрывчат|мета|меф)|"
    r"как\s+(сделать|сварить|синтезировать)\s+(наркотик|взрывчатк|бомб|метамфетамин)|"
    r"детск\w*\s+порно|изнасиловани|"
    r"how\s+to\s+(make|cook|synthesize)\s+(meth|cocaine|heroin|bomb|explosive)|"
    r"child\s+porn|circumvent\s+sanctions\s+to\s+build\s+a\s+bomb)",
    re.IGNORECASE,
)

_model = None  # lazy Detoxify


def _load() -> dict:
    try:
        with open(FILTER_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (FileNotFoundError, ValueError):
        d = {}
    d.setdefault("enabled", True)
    d.setdefault("exempt", [])
    return d


def _save(d: dict) -> None:
    tmp = FILTER_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, FILTER_PATH)


def is_exempt(user_id: int, admin_id: int) -> bool:
    if int(user_id) == int(admin_id):
        return True
    return int(user_id) in [int(x) for x in _load().get("exempt", [])]


def protection_on() -> bool:
    return bool(_load().get("enabled", True))


def set_protection(on: bool) -> dict:
    d = _load()
    d["enabled"] = bool(on)
    _save(d)
    return d


def set_exempt(user_id: int, exempt: bool = True) -> dict:
    d = _load()
    ids = [int(x) for x in d.get("exempt", [])]
    if exempt and int(user_id) not in ids:
        ids.append(int(user_id))
    if not exempt:
        ids = [x for x in ids if x != int(user_id)]
    d["exempt"] = ids
    _save(d)
    return d


def _detoxify_score(text: str) -> float:
    """Max toxicity score via Detoxify, or -1 if unavailable."""
    global _model
    try:
        if _model is None:
            from detoxify import Detoxify
            _model = Detoxify("original")
        res = _model.predict(text[:2000])
        keys = ("toxicity", "severe_toxicity", "obscene",
                "threat", "insult", "identity_attack", "sexual_explicit")
        return max(float(res.get(k, 0.0)) for k in keys)
    except Exception as e:
        logger.debug("detoxify unavailable: %s", e)
        return -1.0


def check(text: str, user_id: int, admin_id: int) -> tuple[bool, str]:
    """Return (blocked, reason). blocked=True means refuse."""
    if not protection_on() or is_exempt(user_id, admin_id):
        return False, ""
    t = text or ""
    if BLOCKED_RE.search(t):
        return True, "запрещённая тема (блок-лист)"
    if _detoxify_score(t) >= 0.85:
        return True, "токсичный контент (Detoxify)"
    return False, ""
