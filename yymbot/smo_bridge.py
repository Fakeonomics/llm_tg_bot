"""Bridge: documents/photos -> smolagents executor (ready engine, 29k stars).

Ready yymbot handles chat; file tasks needing EXECUTION go here (its
plugins can't fire on backends without function-calling). Fail-open:
any bridge error falls through to yymbot's native handler.
"""
import asyncio
import logging
import os
import re

logger = logging.getLogger(__name__)

BOT_USERNAME = os.getenv("BOT_USERNAME", "cmonjustdoitbot").lower()


def _allowed(uid) -> bool:
    try:
        import config as _C
        wl = _C.whitelist
        return (not wl) or str(uid) in [str(x) for x in wl]
    except Exception:
        return True


def _inbox(uid, name) -> str:
    from src.services.agent_tools import WORKDIR
    safe = re.sub(r"[^A-Za-z0-9_.\- ]+", "_", os.path.basename(name or "file"))[:80] or "file"
    tag = f"u{abs(hash(str(int(uid)))) % 10**8:08d}"
    rel = os.path.join("inbox", tag, safe)
    ap = os.path.abspath(os.path.join(WORKDIR, rel))
    if ap != WORKDIR and not ap.startswith(WORKDIR + os.sep):
        raise ValueError("bad name")
    os.makedirs(os.path.dirname(ap), exist_ok=True)
    return ap, rel


async def send_answer(msg, text: str) -> None:
    """Robust send: big code -> .py file, long text -> split, fail -> .txt."""
    t = (text or "").strip() or "failed to answer."
    m = re.search(r"```(?:python|py)?\n(.*?)```", t, re.DOTALL)
    if m and len(m.group(1)) > 1500:
        try:
            import io as _io
            bio = _io.BytesIO(m.group(1).encode("utf-8", "replace"))
            bio.name = "code.py"
            await msg.reply_document(document=bio, filename="code.py",
                                     caption=t[:200])
            return
        except Exception:
            pass
    chunks = [t[i:i + 3800] for i in range(0, len(t), 3800)] or [t]
    for ch in chunks:
        try:
            await msg.reply_text(ch)
            continue
        except Exception:
            pass
        try:
            import asyncio as _aioR
            await _aioR.sleep(2)
            await msg.reply_text(ch)
        except Exception:
            try:
                import io as _io2
                bio = _io2.BytesIO(ch.encode("utf-8", "replace"))
                bio.name = "answer.txt"
                await msg.reply_document(document=bio, filename="answer.txt")
            except Exception:
                pass


async def send_final(msg, text: str) -> None:
    """Routed final: plain prose -> pooled sendMessage; structured -> rich.

    Rich -> plain fallback preserved. Logs delivery class + time so any
    gap between ready answer and delivery is visible per message.
    """
    import time as _tmf
    import logging as _lgf
    t = (text or "").strip()
    if not t:
        return
    t0 = _tmf.monotonic()
    via = "plain"
    if len(t) < 30000:
        try:
            import asyncio as _aiof
            from src.services import richdraft as _rdf
            if _rdf.has_table(t) or _rdf.classify(t) == "rich":
                # True-rich first: typed JSON blocks (real tables etc).
                blocks = _rdf.md_to_blocks(t)
                if blocks and await _aiof.to_thread(
                        _rdf.send_rich_blocks, msg.chat.id, blocks):
                    via = "rich-blocks"
                elif await _aiof.to_thread(_rdf.send_rich_markdown,
                                           msg.chat.id, t):
                    via = "rich-md"
                else:
                    await send_answer(msg, t)
            else:
                await send_answer(msg, t)
        except Exception:
            await send_answer(msg, t)
    else:
        await send_answer(msg, t)
    try:
        _lgf.getLogger("smo_bridge").info(
            "send_final via=%s %.1fs len=%d", via,
            _tmf.monotonic() - t0, len(t))
    except Exception:
        pass




CANCEL: dict = {}
RUNNING: dict = {}
_LAST_VISION_ERR = ""
_USER_LOCKS: dict = {}
STOP_WORDS = {"отмена", "cancel", "стоп", "stop"}
_GREET_RE = None


def _touch_last(d: dict, key: int, val: float) -> None:
    """Bounded registry (drop oldest half past 2000 entries)."""
    try:
        d[int(key)] = val
        if len(d) > 2000:
            for k in list(d.keys())[:1000]:
                d.pop(k, None)
    except Exception:
        pass


# Anti-spam cooldowns (seconds) stamped after each ANSWERED run.
# No daily limits — just no machine-gun requests burning tokens.
# Tunable via env /setcd (admin): CD_PRIVATE / CD_GROUP.
COOLDOWN_PRIVATE = 15.0
COOLDOWN_GROUP = 20.0
_LAST_DONE: dict = {}


def _cd(which: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(which, "") or default))
    except Exception:
        return default


def _cooldown_left(uid, cd: float) -> float:
    try:
        import time as _tmC
        last = _LAST_DONE.get(int(uid), 0.0)
        return max(0.0, cd - (_tmC.monotonic() - last))
    except Exception:
        return 0.0


def _user_lock(uid):
    """Per-user fair queue: one active run each, others get [busy]."""
    import asyncio as _aio
    try:
        key = int(uid)
    except Exception:
        key = 0
    lk = _USER_LOCKS.get(key)
    if lk is None:
        lk = _aio.Lock()
        _USER_LOCKS[key] = lk
    return lk


def _is_stale(msg, max_age_s: int = 600) -> bool:
    """Drop backlog updates (storm/reconnect/restart pileups).

    Stale mentions triggering full agent runs jam the queue for everyone.
    """
    try:
        import datetime as _dt
        d = getattr(msg, "date", None)
        if d is None:
            return False
        now = _dt.datetime.now(_dt.timezone.utc)
        return (now - d).total_seconds() > max_age_s
    except Exception:
        return False


def is_greeting(text: str) -> bool:
    import re as _re
    global _GREET_RE
    if _GREET_RE is None:
        _GREET_RE = _re.compile(
            r"^(привет|здравствуй|здравствуйте|добрый день|добрый вечер|"
            r"доброе утро|hello|hi|hey|йо|салам|прив|здаров|хай|салют|"
            r"добрый|hello there|hi there)[.!?…\s]*$",
            _re.IGNORECASE)
    return bool(_GREET_RE.match((text or "").strip()))


async def direct_answer(msg, text: str):
    """Fast path for greetings: single completion, no agent loop."""
    try:
        from openai import AsyncOpenAI as _OAI
        import os as _os
        from src.services import llm_config as _lcD
        _bd, _kd, _md = _lcD.get_active()
        cli = _OAI(base_url=_bd, api_key=_kd, timeout=60)
        r = await cli.chat.completions.create(
            model=_md or "local",
            messages=[{"role": "system",
                        "content": "Reply in the user's language."},
                      {"role": "user", "content": text}])
        out = ((r.choices[0].message.content) or "").strip()
        if out:
            await send_answer(msg, out)
            return True
    except Exception:
        pass
    return False


async def check_cancel(msg, uid, cid, text: str) -> bool:
    """If text is a stop word and a run is active here: stop it, confirm."""
    try:
        if (text or "").strip().lower() not in STOP_WORDS:
            return False
        ev = RUNNING.get(int(cid))
        if ev is None:
            return False
        try:
            ev.set()
        except Exception:
            pass
        try:
            await msg.reply_text("`[stopped]`", parse_mode="MarkdownV2")
        except Exception:
            try:
                await msg.reply_text("[stopped]")
            except Exception:
                pass
        return True
    except Exception:
        return False


async def rate_cb(update, context) -> None:
    """+/- feedback: log only (no memory pollution)."""
    try:
        q = getattr(update, "callback_query", None)
        data = (getattr(q, "data", None) or "")
        if not data.startswith("rate:"):
            return
        try:
            import logging as _lg
            u = getattr(update, "effective_user", None)
            _lg.getLogger("smo_bridge").info(
                "rating %s from %s",
                data.split(":", 1)[1],
                u.id if u else 0)
            await q.answer("Thanks for the feedback!")
        except Exception:
            pass
    except Exception:
        pass


async def _rate_kb():
    try:
        from telegram import InlineKeyboardButton as _B
        from telegram import InlineKeyboardMarkup as _M
        return _M([[ _B("+", callback_data="rate:good"),
                     _B("-", callback_data="rate:bad") ]])
    except Exception:
        return None

async def cancel_cb(update, context) -> None:
    """Cancel button: owner or admin only (strangers can't kill runs)."""
    try:
        q = getattr(update, "callback_query", None)
        data = (getattr(q, "data", None) or "")
        if not data.startswith("cancel:"):
            return
        fu = getattr(update, "effective_user", None)
        tapper = fu.id if fu else 0
        item = CANCEL.pop(data.split(":", 1)[1], None)
        if item is None:
            try:
                await q.answer("Nothing to stop (already done).")
            except Exception:
                pass
            return
        ev, owner = item if isinstance(item, tuple) else (item, tapper)
        try:
            from src.services import access as _ac
            admin = _ac.is_admin(tapper)
        except Exception:
            admin = False
        if int(tapper) != int(owner) and not admin:
            try:
                await q.answer("Not your task.")
            except Exception:
                pass
            return
        try:
            ev.set()
        except Exception:
            pass
        try:
            await q.answer("Stopping…")
        except Exception:
            pass
    except Exception:
        pass


async def _drain_outbox(msg, uid, cid) -> None:
    """Send files the agent queued via send_file tool, then clear."""
    try:
        from src.services import smo_agent as _smoD
        paths = _smoD.drain_outbox(uid, cid)
    except Exception:
        return
    for p in paths or []:
        try:
            with open(p, "rb") as f:
                data = f.read()
            import io as _ioD
            bio = _ioD.BytesIO(data)
            bio.name = (p.split("/")[-1] or "file")[:80]
            await msg.reply_document(document=bio, filename=bio.name)
        except Exception:
            continue


async def agent_turn(msg, context, user_text: str, uid: int, cid,
                     save_on: bool):
    """Native-draft agent turn (no message statuses, no buttons).

    Thinking/progress via sendRichMessageDraft (ephemeral, auto-expire).
    Cancel by sending stop word (RUNNING registry). Typing as backup.
    """
    import asyncio as _aio
    import random as _rnd
    from src.services import smo_agent as _smo
    from src.services import savectl as _sv
    from src.services.agent_loop import deep_scrub as _scrub
    from src.services.status import run_staged, typing_keepalive
    from src.services import richdraft as _rd
    sink = []
    user_text = (user_text or "")[:4000]
    stop_ev = _aio.Event()
    cancel_ev = _aio.Event()
    RUNNING[int(cid)] = cancel_ev
    lk = _user_lock(uid)
    if lk.locked():
        RUNNING.pop(int(cid), None)
        try:
            await msg.reply_text("`[busy — one at a time]`",
                                 parse_mode="MarkdownV2")
        except Exception:
            pass
        from telegram.ext import ApplicationHandlerStop as _StopBusy
        raise _StopBusy()
    await lk.acquire()
    draft_id = _rnd.randint(1, 2**31 - 1)

    async def _run():
        return await _aio.to_thread(
            _smo.run_task, user_text, uid, cid, save_on, 180, sink)

    async def _post(t, pm=None):
        # Drafts are private-only (docs); groups skip the wasted call.
        # Fire-and-forget: never block the answer on a stormy handshake.
        if getattr(getattr(msg, "chat", None), "type", "") != "private":
            return draft_id
        try:
            import asyncio as _aioe
            fut = _aioe.get_running_loop().run_in_executor(
                None, _rd.draft_thinking, msg.chat.id, draft_id)

            def _swallow(f):
                try:
                    f.exception()
                except Exception:
                    pass

            fut.add_done_callback(_swallow)
        except Exception:
            pass
        return draft_id

    async def _edit(h, t, pm=None):
        if getattr(getattr(msg, "chat", None), "type", "") != "private":
            return
        try:
            import asyncio as _aioe2
            await _aioe2.to_thread(
                _rd.draft_markdown, msg.chat.id, draft_id, t[:1500])
        except Exception:
            pass

    async def _remove(h):
        return

    async def _finalize(t):
        return

    async def _typeit():
        try:
            from telegram.constants import ChatAction as _CA
            await context.bot.send_chat_action(
                chat_id=msg.chat.id, action=_CA.TYPING)
        except Exception:
            pass

    tk = _aio.ensure_future(typing_keepalive(_typeit, stop_ev))
    try:
        try:
            out = await run_staged(_post, _edit, _remove, _finalize,
                                   _run, sink, cancel_event=cancel_ev)
        except _aio.CancelledError:
            try:
                await msg.reply_text("`[stopped]`", parse_mode="MarkdownV2")
            except Exception:
                try:
                    await msg.reply_text("[stopped]")
                except Exception:
                    pass
            from telegram.ext import ApplicationHandlerStop as _StopCancel
            raise _StopCancel()
        except Exception:
            out = ""
        try:
            from src.services.agent_loop import is_broken as _brk
            if _brk(out):
                from openai import AsyncOpenAI as _OAI
                import os as _os
                from src.services import llm_config as _lcF
                _bf, _kf, _mf = _lcF.get_active()
                _cli = _OAI(base_url=_bf, api_key=_kf, timeout=120)
                _r = await _cli.chat.completions.create(
                    model=_mf or "local",
                    messages=[{"role": "system", "content":
                               "Answer briefly in the user's language."},
                              {"role": "user", "content": user_text}],
                    temperature=0.2)
                out = (_r.choices[0].message.content or "").strip() or out
                from src.services.agent_loop import is_broken as _brk2
                if _brk2(out):
                    from src.services import llm_config as _lcX
                    ex = _lcX.get_extra()
                    if ex:
                        from openai import AsyncOpenAI as _OX
                        _cx = _OX(base_url=ex[0], api_key=ex[1],
                                  timeout=120)
                        _rx = await _cx.chat.completions.create(
                            model=ex[2],
                            messages=[{"role": "user",
                                       "content": user_text}],
                            temperature=0.2)
                        out = ((_rx.choices[0].message.content
                                or "").strip() or out)
        except Exception:
            pass
        try:
            out = _smo.final_clean(out or "")
        except Exception:
            pass
        out = _scrub(out or "")
        if save_on and out.strip():
            try:
                _smo.remember(uid, cid, user_text, out)
            except Exception:
                pass
        return out
    finally:
        RUNNING.pop(int(cid), None)
        try:
            lk.release()
        except Exception:
            pass
        try:
            stop_ev.set()
        except Exception:
            pass
        try:
            tk.cancel()
        except Exception:
            pass


async def smo_document(update, context):
    from telegram.ext import ApplicationHandlerStop
    msg = getattr(update, "message", None)
    doc = getattr(msg, "document", None)
    if not doc:
        return
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    try:
        if (doc.file_size or 0) > 20_000_000:
            await msg.reply_text("`[file too large (20MB max)]`", parse_mode="MarkdownV2")
            raise ApplicationHandlerStop()
        ap, rel = _inbox(uid, doc.file_name)
        tgfile = await context.bot.get_file(doc.file_id)
        await tgfile.download_to_drive(ap)
        cap = (msg.caption or "").strip()
        from src.services import smo_agent as _smo
        from src.services import savectl as _sv
        text = (f"[Файл: {rel}]\nПрочитай инструментом и выполни просьбу. "
                f"Подпись: {cap or '(нет)'}")
        out = await agent_turn(msg, context, text, uid, uid,
                               _sv.is_save_on(uid))
        out = out or "failed to process file."
        await send_final(msg, out)
    except ApplicationHandlerStop:
        raise
    except Exception as e:
        from src.services.status import brief_net_error as _bneD
        _bneD(logger, "smo_document", e)
        return
    raise ApplicationHandlerStop()


async def _vision_answer(path: str, caption: str):
    """Direct multimodal call (any model; mmproj succeeds, text-only 400s).

    No configs dependency (explicit base_url). Returns text or ''.
    Downscales to 1024px (huge photos timeout on slow hardware) and logs
    the REAL failure reason (was silently swallowed -> mystery OCR).
    """
    import logging as _lg
    log = _lg.getLogger("smo_bridge")
    try:
        import base64 as _b64
        from openai import AsyncOpenAI as _OAI
        import os as _os
        try:
            from src.services import llm_config as _lcV0
            _bv0, _, _ = _lcV0.get_active()
            base = _bv0 or _os.getenv("LLAMA_BASE_URL", "http://localhost:8080/v1")
        except Exception:
            base = _os.getenv("LLAMA_BASE_URL", "http://localhost:8080/v1")
        try:
            from src.services import llama_probe as _pb
            _p = _pb.probe_llama(base)
            log.info("vision probe: model=%s has_vision=%s",
                     str(_p.get("active_model_id", ""))[-50:],
                     _p.get("has_vision"))
        except Exception:
            pass
        try:
            from PIL import Image as _IMG
            import io as _bio
            im = _IMG.open(path).convert("RGB")
            im.thumbnail((1024, 1024))
            buf = _bio.BytesIO()
            im.save(buf, "JPEG", quality=85)
            raw = buf.getvalue()
        except Exception:
            with open(path, "rb") as f:
                raw = f.read()
        b64 = _b64.b64encode(raw).decode()
        from src.services import llm_config as _lcV
        _bv, _kv, _mv = _lcV.get_active()
        client = _OAI(base_url=_bv or base, api_key=_kv, timeout=120)
        r = await client.chat.completions.create(
            model=_mv or "local",
            messages=[
                {"role": "system",
                 "content": "Reply in the user's language."},
                {"role": "user", "content": [
                    {"type": "text",
                     "text": caption or "Что на изображении? Опиши."},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/jpeg;base64," + b64}}]}],
            temperature=0.2)
        return ((r.choices[0].message.content) or "").strip()
    except Exception as e:
        global _LAST_VISION_ERR
        _LAST_VISION_ERR = f"{type(e).__name__}: {e}"[:200]
        log.warning("vision direct failed (%s), OCR next",
                    _LAST_VISION_ERR)
        return ""


async def smo_photo(update, context):
    from telegram.ext import ApplicationHandlerStop
    msg = getattr(update, "message", None)
    if not msg or not getattr(msg, "photo", None):
        return
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    try:
        photo = msg.photo[-1]
        import tempfile as _tf
        with _tf.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            tmp = tf.name
        try:
            tgfile = await context.bot.get_file(photo.file_id)
            await tgfile.download_to_drive(tmp)
            cap = (msg.caption or "").strip()
            answer = await _vision_answer(tmp, cap)
            if answer:
                from src.services.agent_loop import deep_scrub as _scrv
                from src.services import smo_agent as _smv
                from src.services import savectl as _svv
                answer = _scrv(answer)
                await send_answer(msg, answer)
                try:
                    if _svv.is_save_on(uid):
                        _smv.remember(uid, uid, cap or "[photo]", answer)
                except Exception:
                    pass
                raise ApplicationHandlerStop()
            from src.services import ocr as _ocr
            if not _ocr.tesseract_available():
                why = (_LAST_VISION_ERR or "unknown")[:160]
                try:
                    await msg.reply_text("[no vision "
                                         f"({why}). install tesseract]")
                except Exception:
                    pass
                raise ApplicationHandlerStop()
            import asyncio as _aioO
            text = await _aioO.to_thread(_ocr.ocr_image, tmp)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if not text:
            await msg.reply_text("`[could not read image text]`", parse_mode="MarkdownV2")
            raise ApplicationHandlerStop()
        cap = (msg.caption or "").strip()
        from src.services import savectl as _svp
        out = await agent_turn(
            msg, context,
            f"[OCR]:\n{text[:2500]}\nВопрос: {cap or 'Что на изображении?'}",
            uid, uid, _svp.is_save_on(uid))
        out = out or "failed to answer."
        await send_answer(msg, out)
    except ApplicationHandlerStop:
        raise
    except Exception as e:
        from src.services.status import brief_net_error as _bneP
        _bneP(logger, "smo_photo", e)
        return
    raise ApplicationHandlerStop()


_LAST_GROUP = {}
_LAST_PRIVATE = {}


async def smo_private(update, context):
    """Private text -> full agent (real tools, so no hallucinated calls).

    Fail-open: any error falls through to yymbot native handler.
    """
    from telegram.ext import ApplicationHandlerStop
    msg = getattr(update, "message", None)
    if not msg:
        return
    chat = getattr(msg, "chat", None)
    if not chat or getattr(chat, "type", "") != "private":
        return
    text = ((getattr(msg, "text", None) or getattr(msg, "caption", None)) or "")
    if not text.strip() or text.strip().startswith("/"):
        return
    if _is_stale(msg):
        raise ApplicationHandlerStop()
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    import time as _tm0
    t_start = _tm0.monotonic()
    try:
        import datetime as _dt, logging as _lg
        lag = (_dt.datetime.now(_dt.timezone.utc) - msg.date).total_seconds()
        _lg.getLogger("smo_bridge").info("private lag %.1fs uid=%s",
                                         lag, uid)
    except Exception:
        pass
    if is_greeting(text):
        try:
            if await direct_answer(msg, text):
                raise ApplicationHandlerStop()
        except ApplicationHandlerStop:
            raise
        except Exception:
            pass
    import time as _tm
    now = _tm.monotonic()
    if now - _LAST_PRIVATE.get(int(uid), 0.0) < 2.0:
        try:
            await msg.reply_text("`[slow down]`", parse_mode="MarkdownV2")
        except Exception:
            pass
        raise ApplicationHandlerStop()
    _touch_last(_LAST_PRIVATE, uid, now)
    if await check_cancel(msg, uid, chat.id, text):
        raise ApplicationHandlerStop()
    left = _cooldown_left(uid, _cd("CD_PRIVATE", COOLDOWN_PRIVATE))
    if left > 0:
        try:
            await msg.reply_text(f"`[wait {int(left) + 1}s]`",
                                 parse_mode="MarkdownV2")
        except Exception:
            pass
        raise ApplicationHandlerStop()
    try:
        from src.services import savectl as _svp
        save = _svp.is_save_on(uid)
    except Exception:
        save = True
    try:
        final = await agent_turn(msg, context, text, uid, chat.id, save)
    except ApplicationHandlerStop:
        raise
    except Exception:
        return
    if not (final or "").strip():
        try:
            await msg.reply_text("[model backend unreachable, try later]")
        except Exception:
            pass
        raise ApplicationHandlerStop()
    try:
        await send_final(msg, final)
    except Exception:
        return
    try:
        await _drain_outbox(msg, uid, chat.id)
    except Exception:
        pass
    _touch_last(_LAST_DONE, uid, _tm0.monotonic())
    try:
        import logging as _lg2
        _lg2.getLogger("smo_bridge").info("private done %.1fs uid=%s",
                                          _tm0.monotonic() - t_start, uid)
    except Exception:
        pass
    raise ApplicationHandlerStop()


async def smo_group_mention(update, context):
    """Group @mention/reply -> full agent (thinking display + tools + memory).

    Same rich experience as before; yymbot native group flow stays as
    fallback (fail-open: any error falls through to it).
    """
    from telegram.ext import ApplicationHandlerStop
    msg = getattr(update, "message", None)
    if not msg:
        return
    chat = getattr(msg, "chat", None)
    if not chat or getattr(chat, "type", "") not in ("group", "supergroup"):
        return
    text = ((getattr(msg, "text", None) or getattr(msg, "caption", None)) or "")
    if not text.strip():
        return
    if _is_stale(msg):
        raise ApplicationHandlerStop()
    me = BOT_USERNAME
    mentioned = f"@{me}".lower() in text.lower()
    replied = False
    try:
        r = getattr(msg, "reply_to_message", None)
        ru = getattr(r, "from_user", None) if r else None
        replied = bool(ru and getattr(ru, "is_bot", False)
                       and (getattr(ru, "username", "") or "").lower() == me.lower())
    except Exception:
        pass
    if not (mentioned or replied):
        return
    uid = update.effective_user.id
    if not _allowed(uid):
        return
    now = __import__("time").monotonic()
    if now - _LAST_GROUP.get(int(uid), 0.0) < 3.0:
        try:
            await msg.reply_text("`[slow down]`", parse_mode="MarkdownV2")
        except Exception:
            pass
        raise ApplicationHandlerStop()
    _touch_last(_LAST_GROUP, uid, now)
    if await check_cancel(msg, uid, chat.id, text):
        raise ApplicationHandlerStop()
    _greet_check = text.replace(f"@{me}", "").strip()
    if is_greeting(_greet_check):
        try:
            if await direct_answer(msg, _greet_check):
                raise ApplicationHandlerStop()
        except ApplicationHandlerStop:
            raise
        except Exception:
            pass
    try:
        clean = text.replace(f"@{me}", "").strip() or text
        left = _cooldown_left(uid, _cd("CD_GROUP", COOLDOWN_GROUP))
        if left > 0:
            try:
                await msg.reply_text(f"`[wait {int(left) + 1}s]`",
                                     parse_mode="MarkdownV2")
            except Exception:
                pass
            raise ApplicationHandlerStop()
        from src.services import savectl as _svg
        final = await agent_turn(msg, context, clean, uid, chat.id,
                                 _svg.is_save_on(uid))
    except ApplicationHandlerStop:
        raise
    except Exception:
        return
    if not (final or "").strip():
        try:
            await msg.reply_text("[model backend unreachable, try later]")
        except Exception:
            pass
        raise ApplicationHandlerStop()
    try:
        await send_final(msg, final)
    except Exception:
        return
    try:
        await _drain_outbox(msg, uid, chat.id)
    except Exception:
        pass
    _touch_last(_LAST_DONE, uid, __import__("time").monotonic())
    raise ApplicationHandlerStop()
