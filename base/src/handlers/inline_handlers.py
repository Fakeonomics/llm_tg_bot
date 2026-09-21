"""Inline new-mode: instant placeholder -> chosen_inline_result -> full agent -> edit.

Old-mode (precompute in popup) times out on slow local models. New-mode posts
a placeholder FAST, runs the full agent only after the user SENDS it (chosen),
then edits the message with the real answer. Needs BotFather /setinline +
/setinlinefeedback. Tiers (5/100) counted on queries; agent runs on send.
"""
import logging
import time
import uuid

from aiogram import Bot, F, Router
from aiogram.types import (
    CallbackQuery, ChosenInlineResult, InlineKeyboardButton,
    InlineKeyboardMarkup, InlineQuery, InlineQueryResultArticle,
    InputTextMessageContent,
)

from src.config import configs
from src.config.config import MAX_TELEGRAM_MESSAGE_LEN
from src.handlers.user_handlers import dialog_manager
from src.services import access as access_ctl
from src.services import inline_limits as lim
from src.services import moderation as mod
from src.services import savectl
from src.services.safeformat import format_one

logger: logging.Logger = logging.getLogger(__name__)
router = Router()

_INLINE_LAST: dict[int, float] = {}


@router.inline_query()
async def process_inline(inline: InlineQuery):
    uid = inline.from_user.id
    access_ctl.bootstrap_admin(uid)
    if not access_ctl.is_allowed(uid):
        await inline.answer([], cache_time=5,
                            switch_pm_text="Нет доступа",
                            switch_pm_parameter="no_access")
        return
    ok, limit, _used = lim.allow(uid, access_ctl.is_admin(uid))
    if not ok:
        await inline.answer(
            [], cache_time=10,
            switch_pm_text=f"Лимит {limit}/час. Жми — подниму до 100",
            switch_pm_parameter="raise_limit")
        return
    now = time.monotonic()
    if (not access_ctl.is_admin(uid)
            and now - _INLINE_LAST.get(uid, 0.0) < 4.0):
        await inline.answer([], cache_time=2,
                            switch_pm_text="Подожди пару секунд",
                            switch_pm_parameter="cooldown")
        return
    _INLINE_LAST[uid] = now
    query = (inline.query or "").strip()
    if not query:
        await inline.answer([], cache_time=5,
                            switch_pm_text="Введи вопрос",
                            switch_pm_parameter="help")
        return
    admin_id = access_ctl.get_admin_id()
    blocked, _ = mod.check(query, uid, admin_id)
    if blocked:
        await inline.answer([], cache_time=5,
                            switch_pm_text="Заблокировано защитой",
                            switch_pm_parameter="blocked")
        return
    import asyncio
    from src.services import smo_agent as smo
    from src.services.agent_loop import deep_scrub, is_broken
    answer = ""
    try:
        answer = await asyncio.wait_for(
            asyncio.to_thread(smo.run_task, query, uid, uid,
                              savectl.is_save_on(uid), 30),
            timeout=10)
    except Exception as e:
        logger.debug("inline agent slow/failed, fallback direct: %s", e)
    if is_broken(answer):
        try:
            chat = await dialog_manager.get_or_create_chat(uid, uid)
            answer = await chat.get_answer(query)
            savectl.enforce_in_place(chat)
        except Exception:
            answer = ""
    answer = deep_scrub(answer or "")
    if not answer.strip():
        answer = "Не получилось ответить."
    blocked_out, _ = mod.check(answer, uid, admin_id)
    if blocked_out:
        await inline.answer([], cache_time=5,
                            switch_pm_text="Заблокировано защитой",
                            switch_pm_parameter="blocked")
        return
    sent = f"❓ {query[:300]}\n\n💬 {answer}"
    safe_text, pm = format_one(sent[:3800])
    result = InlineQueryResultArticle(
        id=str(uuid.uuid4()),
        title="Ответ агента",
        description=answer[:120],
        input_message_content=InputTextMessageContent(
            message_text=safe_text, parse_mode=pm),
    )
    await inline.answer([result], cache_time=5, is_personal=True)
