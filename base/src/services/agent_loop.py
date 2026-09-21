"""Agent delivery + privacy scrub. Engine: smolagents CodeAgent (29k★).

Thinking/acting status, moderation, safe send, and output scrubbing live
here. The tool loop itself is smolagents (battle-tested), not custom code.
"""
import logging
import os
import re

logger: logging.Logger = logging.getLogger(__name__)


def short_id(mid: str) -> str:
    """Basename only: no dirs, no user, no host. Never leaks paths."""
    base = os.path.basename(str(mid or "").strip())
    return base or "model"


def scrub(text: str) -> str:
    """Strip user home dirs and long model paths from chat-bound text."""
    t = str(text or "")
    for home in ("/var/home/yuri", "/home/yuri"):
        t = t.replace(home, "~")
    t = re.sub(r"[\w\-./~]*\.gguf\b",
               lambda m: os.path.basename(m.group(0)), t)
    return t


_ID_CACHE = {"t": 0.0, "tokens": set()}
# Static hide: ONLY where/how markers (hosts/ports/setup words).
# Model names are ALLOWED openly (user order) — never added here.
_STATIC_HIDE = {"lmstudio", "localhost", "8080"}


def _identity_tokens():
    """Only where/how markers (hosts/ports/setup). Model names allowed."""
    return set(_STATIC_HIDE)


def deep_scrub(text: str) -> str:
    """scrub() + remove model-identity tokens. For all chat-bound output."""
    t = scrub(text)
    for tok in _identity_tokens():
        try:
            pat = tok if "[-_.]" in tok else re.escape(tok)
            t = re.sub(pat, "[скрыто]", t, flags=re.IGNORECASE)
        except Exception:
            continue
    return t


def is_broken(text: str) -> bool:
    """Garbled agent output (parse-error meta-talk, traces). Needs fallback."""
    t = (text or "").strip()
    if not t:
        return True
    if ("Code parsing failed" in t
            or "Make sure to provide correct code" in t
            or "invalid, because the regex" in t
            or "provide correct code blobs" in t):
        return True
    if "Calling tools:" in t and "function" in t:
        return True
    if "Thoughts:" in t and "<code>" in t:
        return True
    if "<code>" in t or "</code>" in t:
        return True
    if "<|tool_call_start|>" in t and "<|tool_call_end|>" in t:
        return True
    return False


async def deliver(message, user_text: str, admin_id: int, save_on: bool = True):
    """smolagents turn with live thinking/acting status.

    Sends 'thinking', edits per agent step, deletes status, moderates +
    safe-sends final. Returns final (or None if refused/failed).
    """
    import asyncio
    from src.services import moderation as mod
    from src.services import smo_agent as smo
    from src.services.safeformat import format_chunks

    uid = message.from_user.id
    cid = message.chat.id
    try:
        status = await message.reply("Thinking…")
    except Exception:
        status = None
    sink: list = []

    async def _run():
        return await asyncio.to_thread(
            smo.run_task, user_text, uid, cid, save_on, 180, sink)

    task = asyncio.ensure_future(_run())
    shown = ""
    try:
        while not task.done():
            await asyncio.sleep(2.0)
            if status and sink and sink[-1] != shown:
                shown = sink[-1]
                try:
                    await status.edit_text(shown[:200])
                except Exception:
                    pass
        final = await task
        if is_broken(final):
            from src.config import configs as _cfg
            from src.services.openai_api import complete as _complete
            try:
                final = await _complete(
                    [{"role": "system", "content":
                      "Answer concisely in the user's language."},
                     {"role": "user", "content": user_text}],
                    _cfg.chat_model)
            except Exception:
                final = "Failed to answer."
    except Exception as e:
        from src.services.status import brief_net_error as _bneA
        _bneA(logger, "agent deliver", e)
        final = "Agent error."
        try:
            task.cancel()
        except Exception:
            pass
    if status:
        try:
            await status.delete()
        except Exception:
            pass
    final = deep_scrub(final or "")
    blocked, _ = mod.check(final, uid, admin_id)
    if blocked:
        await message.reply(mod.REFUSE_RU)
        return None
    sent_any = False
    for chunk_text, pm in format_chunks(final):
        if not (chunk_text or "").strip():
            continue
        try:
            await message.reply(text=chunk_text, parse_mode=pm)
            sent_any = True
        except Exception as e:
            logger.warning("chunk send failed: %s", e)
    if not sent_any:
        try:
            await message.reply("Failed to send the answer.")
        except Exception:
            pass
    else:
        if save_on:
            try:
                from src.services import smo_agent as _smo
                _smo.remember(uid, cid, user_text, final)
            except Exception:
                pass
    return final
