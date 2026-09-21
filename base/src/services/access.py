"""Admin access control.

Mode "all": everyone may use the bot.
Mode "limited": only admin + granted user ids.
Admin (ADMIN_ID env, else first user to contact the bot) can grant/revoke.
State lives in a small JSON file next to .env.
"""
import json
import logging
import os

logger: logging.Logger = logging.getLogger(__name__)

CONFIG_PATH = os.getenv(
    "TGBOT_ACCESS",
    os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "access.json"),
)
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)


def _load() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (FileNotFoundError, ValueError):
        d = {}
    d.setdefault("mode", "all")
    d.setdefault("allowed", [])
    d.setdefault("admin_id", ADMIN_ID)
    return d


def _save(d: dict) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def get_state() -> dict:
    return _load()


def get_admin_id() -> int:
    return int(_load().get("admin_id") or 0)


def bootstrap_admin(user_id: int) -> bool:
    """Set first-contact user as admin if none configured. Returns True if set."""
    d = _load()
    if not d.get("admin_id"):
        d["admin_id"] = int(user_id)
        _save(d)
        logger.info("Bootstrapped admin_id=%s", user_id)
        return True
    return False


def is_admin(user_id: int) -> bool:
    return int(user_id) == get_admin_id()


def is_allowed(user_id: int) -> bool:
    d = _load()
    if is_admin(user_id):
        return True
    if d.get("mode") == "all":
        return True
    return int(user_id) in [int(x) for x in d.get("allowed", [])]


def grant(user_id: int) -> dict:
    d = _load()
    if int(user_id) not in [int(x) for x in d["allowed"]]:
        d["allowed"].append(int(user_id))
        _save(d)
    return d


def revoke(user_id: int) -> dict:
    d = _load()
    d["allowed"] = [x for x in d["allowed"] if int(x) != int(user_id)]
    _save(d)
    return d


def set_mode(mode: str) -> dict:
    if mode not in ("all", "limited"):
        raise ValueError("mode must be 'all' or 'limited'")
    d = _load()
    d["mode"] = mode
    _save(d)
    return d
