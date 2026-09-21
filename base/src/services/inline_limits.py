"""Inline tiers: unseen users 5/hr, started users 100/hr, admin unlimited.

RAM-only counters (sliding hour, temporary). `seen` persists tiny JSON
(user started the bot in PM). Over-limit inline answers carry a
switch_pm button (referral to /start) that raises the tier.
"""
import json
import logging
import os
import time
from collections import deque

logger: logging.Logger = logging.getLogger(__name__)

SEEN_PATH = os.getenv(
    "TGBOT_SEEN",
    os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "seen.json"),
)

LIMIT_UNSEEN = 5
LIMIT_SEEN = 100
WINDOW_S = 3600

_hits: dict[int, deque] = {}


def _load_seen() -> dict:
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (FileNotFoundError, ValueError):
        d = {}
    return d if isinstance(d, dict) else {}


def _save_seen(d: dict) -> None:
    try:
        if len(d) > 1000:
            d = dict(sorted(d.items(), key=lambda kv: kv[1])[-500:])
        tmp = SEEN_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, SEEN_PATH)
    except Exception as e:
        logger.debug("seen save failed: %s", e)


def is_seen(user_id: int) -> bool:
    return str(int(user_id)) in _load_seen()


def mark_seen(user_id: int) -> None:
    d = _load_seen()
    d[str(int(user_id))] = int(time.time())
    _save_seen(d)


def allow(user_id: int, is_admin: bool = False):
    """Return (allowed, limit, used_in_window). Records the hit if allowed."""
    uid = int(user_id)
    if is_admin:
        return True, -1, 0
    limit = LIMIT_SEEN if is_seen(uid) else LIMIT_UNSEEN
    now = time.monotonic()
    dq = _hits.get(uid)
    if dq is None:
        dq = deque()
        _hits[uid] = dq
    while dq and now - dq[0] > WINDOW_S:
        dq.popleft()
    if len(dq) >= limit:
        return False, limit, len(dq)
    dq.append(now)
    if len(_hits) > 500:
        _hits.pop(next(iter(_hits)), None)
    return True, limit, len(dq)
