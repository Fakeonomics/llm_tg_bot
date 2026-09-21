"""Agent handlers: admin commands, group agent, tools, vision/OCR photos.

- Private + group text via mention/reply (base DM handler keeps plain DM).
- /status probes llama.cpp:8080 (model + vision) live.
- Photos: vision model -> direct; else lightweight tesseract OCR -> text.
- Moderation filter applies to input; admin/exempt bypass.
- Tool cmds: /read /search /run (sandboxed, auto-clean).
"""
import base64
import logging
import os
import re
import tempfile
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from src.config import configs
from src.config.config import MAX_TELEGRAM_MESSAGE_LEN
from src.handlers.helpers import debug_handler_reply
from src.handlers.user_handlers import dialog_manager
from src.services import access as access_ctl
from src.services import agent_tools as tools
from src.services import llama_probe as probe
from src.services import moderation as mod
from src.services import ocr as ocr_svc
from src.services import savectl
from src.services.agent_loop import deliver
from src.services.llm_queue import QUEUE
from src.services.messages import split_message
from src.services.safeformat import format_chunks, format_one
from src.services.openai_api import complete_with_image

logger: logging.Logger = logging.getLogger(__name__)
router = Router()

_probe_cache: dict = {}

# Lightweight per-user cooldown for LLM paths (RAM only). Admin exempt.
_LAST_CALL: dict[int, float] = {}
COOLDOWN_S = 4.0


def _cooldown_ok(user_id: int) -> bool:
    if access_ctl.is_admin(user_id):
        return True
    now = time.monotonic()
    last = _LAST_CALL.get(int(user_id), 0.0)
    if now - last < COOLDOWN_S:
        return False
    _LAST_CALL[int(user_id)] = now
    return True


def refresh_probe() -> dict:
    global _probe_cache
    _probe_cache = probe.probe_llama(
        os.getenv("LLAMA_BASE_URL", "http://localhost:8080/v1"))
    return _probe_cache


def _deny(message: Message) -> bool:
    return not access_ctl.is_allowed(message.from_user.id)


def _filtered(message: Message, text: str) -> str | None:
    blocked, _ = mod.check(text, message.from_user.id,
                           access_ctl.get_admin_id())
    return mod.REFUSE_RU if blocked else None


@router.message(Command("start"))
@debug_handler_reply
async def cmd_start(message: Message):
    access_ctl.bootstrap_admin(message.from_user.id)
    if _deny(message):
        await message.reply("Нет доступа. Попроси администратора.")
        return
    await message.reply(
        "Привет! Я агент: просто напиши задачу или скинь файл — "
        "разберусь сам.\n/help — подробнее.")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip() == "raise_limit":
        from src.services import inline_limits as lim
        lim.mark_seen(message.from_user.id)
        await message.reply(
            "Готово: инлайн-лимит поднят до 100 запросов в час. "
            "Пиши @бот вопрос в любом чате.")


@router.message(Command("status"))
@debug_handler_reply
async def cmd_status(message: Message):
    access_ctl.bootstrap_admin(message.from_user.id)
    if _deny(message):
        await message.reply("Нет доступа.")
        return
    ready = refresh_probe().get("ok", False)
    st = access_ctl.get_state()
    lines = [f"Готов: {'да' if ready else 'нет'}",
             f"Доступ: {st['mode']}",
             f"Защита: {'вкл' if mod.protection_on() else 'выкл'}"]
    await message.reply("\n".join(lines))


def _admin_only(message: Message) -> bool:
    if not access_ctl.is_admin(message.from_user.id):
        return True
    return False


@router.message(Command("grant"))
@debug_handler_reply
async def cmd_grant(message: Message, bot: Bot):
    access_ctl.bootstrap_admin(message.from_user.id)
    if _admin_only(message):
        return
    arg = (message.text or "").split(maxsplit=1)
    if len(arg) < 2 or not arg[1].strip().isdigit():
        await message.reply("Использование: /grant <user_id>")
        return
    access_ctl.grant(int(arg[1].strip()))
    await message.reply(f"Доступ выдан: {arg[1].strip()}")


@router.message(Command("revoke"))
@debug_handler_reply
async def cmd_revoke(message: Message):
    if _admin_only(message):
        return
    arg = (message.text or "").split(maxsplit=1)
    if len(arg) < 2 or not arg[1].strip().isdigit():
        await message.reply("Использование: /revoke <user_id>")
        return
    access_ctl.revoke(int(arg[1].strip()))
    await message.reply(f"Доступ отозван: {arg[1].strip()}")


@router.message(Command("mode"))
@debug_handler_reply
async def cmd_mode(message: Message):
    if _admin_only(message):
        return
    arg = (message.text or "").split(maxsplit=1)
    if len(arg) < 2 or arg[1].strip() not in ("all", "limited"):
        await message.reply("Использование: /mode all|limited")
        return
    access_ctl.set_mode(arg[1].strip())
    await message.reply(f"Режим доступа: {arg[1].strip()}")


@router.message(Command("protect"))
@debug_handler_reply
async def cmd_protect(message: Message):
    if _admin_only(message):
        return
    arg = (message.text or "").split(maxsplit=1)
    if len(arg) < 2 or arg[1].strip() not in ("on", "off"):
        await message.reply("Использование: /protect on|off")
        return
    mod.set_protection(arg[1].strip() == "on")
    await message.reply(f"Защита: {arg[1].strip()}")


@router.message(Command("exempt"))
@debug_handler_reply
async def cmd_exempt(message: Message):
    if _admin_only(message):
        return
    arg = (message.text or "").split(maxsplit=2)
    if len(arg) < 3 or not arg[1].strip().isdigit():
        await message.reply("Использование: /exempt <user_id> on|off")
        return
    mod.set_exempt(int(arg[1].strip()), arg[2].strip() == "on")
    await message.reply(f"exempt {arg[1].strip()} = {arg[2].strip()}")


def _is_group(message: Message) -> bool:
    return (message.chat.type in ("group", "supergroup"))


async def _should_answer_group(message: Message, bot: Bot) -> bool:
    if not _is_group(message):
        return True
    text = message.text or message.caption or ""
    me = await bot.get_me()
    if me.username and f"@{me.username}" in text:
        return True
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id == me.id:
            return True
    return False


@router.message(F.content_type == "photo")
@debug_handler_reply
async def process_photo(message: Message, bot: Bot):
    access_ctl.bootstrap_admin(message.from_user.id)
    if _deny(message):
        await message.reply("Нет доступа.")
        return
    if not _cooldown_ok(message.from_user.id):
        await message.reply("Не так быстро, подожди пару секунд.")
        return
    if _is_group(message) and not await _should_answer_group(message, bot):
        return
    if not _probe_cache:
        refresh_probe()
    caption = message.caption or "Что на этом изображении?"
    if (refusal := _filtered(message, caption)):
        await message.reply(refusal)
        return
    photo = message.photo[-1]
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        tmp = tf.name
    try:
        fi = await bot.get_file(photo.file_id)
        await bot.download_file(fi.file_path, tmp)
        if _probe_cache.get("has_vision"):
            with open(tmp, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            answer = await complete_with_image(
                caption, b64, configs.chat_model)
        else:
            text = ocr_svc.ocr_image(tmp)
            if not text:
                await message.reply(
                    "Нет зрения у модели и нет tesseract. "
                    "Установи tesseract для OCR.")
                return
            chat = await dialog_manager.get_or_create_chat(
                message.from_user.id, message.chat.id)
            answer = await chat.get_answer(
                f"[OCR с изображения]:\n{text}\n\nВопрос: {caption}")
            savectl.enforce_in_place(chat)
    except Exception as e:
        logger.exception("photo error: %s", e)
        await message.reply("Ошибка обработки изображения.")
        return
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if (refusal := _filtered(message, answer)):
        await message.reply(refusal)
        return
    for chunk_text, pm in format_chunks(answer):
        await message.reply(text=chunk_text, parse_mode=pm)


@router.message(F.content_type == "text", F.chat.type.in_({"group", "supergroup"}))
@debug_handler_reply
async def process_agent_text(message: Message, bot: Bot):
    """Group agent text. Base DM handler owns plain private text."""
    access_ctl.bootstrap_admin(message.from_user.id)
    if _deny(message):
        return
    if not _cooldown_ok(message.from_user.id):
        await message.reply("Не так быстро, подожди пару секунд.")
        return
    if (message.text or "").startswith("/"):
        return  # other command handlers
    if _is_group(message):
        if not await _should_answer_group(message, bot):
            return
    else:
        return  # private plain text -> base user_handlers
    if (refusal := _filtered(message, message.text or "")):
        await message.reply(refusal)
        return
    await deliver(message, message.text or "", access_ctl.get_admin_id(),
                  savectl.is_save_on(message.from_user.id))


HELP_TEXT = (
    "Я агент: просто напиши задачу — сам прочитаю файлы, "
    "поищу и выполню. Можно скинуть файл документом.\n"
    "/status — состояние\n"
    "/info — мощности контейнера\n"
    "/clear — забыть контекст\n"
    "В группах отвечаю по упоминанию/реплаю. Inline: @бот вопрос."
)
HELP_ADMIN = "Админ: /grant /revoke /mode /protect /exempt /save /savelist"


@router.message(Command("help"))
@debug_handler_reply
async def cmd_help(message: Message):
    if _deny(message):
        await message.reply("Нет доступа.")
        return
    text = HELP_TEXT
    if access_ctl.is_admin(message.from_user.id):
        text += "\n" + HELP_ADMIN
    await message.reply(text)


@router.message(Command("info"))
@debug_handler_reply
async def cmd_info(message: Message):
    if _deny(message):
        await message.reply("Нет доступа.")
        return
    from src.services.agent_tools import (
        _fake_hw_paths, CONTAINER_CPUS, CONTAINER_MEM_MB,
        CONTAINER_CPU_QUOTA_DESC)
    model, cores = "?", 0
    cpu, _mem = _fake_hw_paths()
    try:
        txt = open(cpu).read() if cpu else ""
        cores = txt.count("processor\t:")
        for line in txt.splitlines():
            if "model name" in line:
                model = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    await message.reply(
        f"Контейнер:\nCPU: {model}\nЯдер видно: {cores} "
        f"(доступно {CONTAINER_CPUS})\n"
        f"RAM на юзера: {CONTAINER_MEM_MB} МБ\n"
        f"CPU: {CONTAINER_CPU_QUOTA_DESC}\n"
        "Файлы и сеть: изолированы, мусор чистится сам.")


@router.message(Command("clear"))
@debug_handler_reply
async def cmd_clear(message: Message):
    if _deny(message):
        await message.reply("Нет доступа.")
        return
    from src.services import smo_agent as smo
    smo.reset_memory(message.from_user.id, message.chat.id)
    dialog_manager.dialog_storage.chats.pop(
        (message.from_user.id, message.chat.id), None)
    await message.reply("Контекст очищен. Начинаем с чистого листа.")


@router.message(Command("save"))
@debug_handler_reply
async def cmd_save(message: Message):
    if _admin_only(message):
        return
    arg = (message.text or "").split()
    if len(arg) < 3 or not arg[1].isdigit() or arg[2] not in ("on", "off"):
        await message.reply("Использование: /save <user_id> on|off")
        return
    savectl.set_save(int(arg[1]), arg[2] == "on")
    state = "сохраняется (память вкл)" if arg[2] == "on" else "stateless (память выкл)"
    await message.reply(f"save {arg[1]} = {state}")


@router.message(Command("savelist"))
@debug_handler_reply
async def cmd_savelist(message: Message):
    if _admin_only(message):
        return
    overrides = savectl.list_saves()
    if not overrides:
        await message.reply("Переопределений нет. По умолчанию save=ON у всех.")
        return
    lines = [f"{uid}: {'ON' if on else 'OFF'}" for uid, on in overrides.items()]
    await message.reply("Сейвы:\n" + "\n".join(lines))


@router.message(F.document)
@debug_handler_reply
async def process_document(message: Message, bot: Bot):
    """File drop -> inbox inside sandbox -> agent sees the path and reads it."""
    access_ctl.bootstrap_admin(message.from_user.id)
    if _deny(message):
        await message.reply("Нет доступа.")
        return
    if not _cooldown_ok(message.from_user.id):
        await message.reply("Не так быстро, подожди пару секунд.")
        return
    if _is_group(message) and not await _should_answer_group(message, bot):
        return
    doc = message.document
    if (doc.file_size or 0) > 20_000_000:
        await message.reply("Файл слишком большой (лимит 20МБ).")
        return
    name = os.path.basename(doc.file_name or "file")
    name = re.sub(r"[^A-Za-z0-9_.\- ]+", "_", name)[:80] or "file"
    rel = os.path.join("inbox", str(message.from_user.id), name)
    from src.services.agent_tools import WORKDIR
    ap = os.path.abspath(os.path.join(WORKDIR, rel))
    if ap != WORKDIR and not ap.startswith(WORKDIR + os.sep):
        await message.reply("Недопустимое имя файла.")
        return
    os.makedirs(os.path.dirname(ap), exist_ok=True)
    try:
        fi = await bot.get_file(doc.file_id)
        await bot.download_file(fi.file_path, ap)
    except Exception as e:
        logger.exception("doc download failed: %s", e)
        await message.reply("Не смог скачать файл.")
        return
    caption = message.caption or ""
    if (refusal := _filtered(message, caption)):
        await message.reply(refusal)
        return
    user_text = (f"[Пользователь прислал файл: {rel}]\n"
                 "Прочитай его инструментом и выполни просьбу. "
                 f"Подпись: {caption or '(нет)'}")
    await deliver(message, user_text, access_ctl.get_admin_id(),
                  savectl.is_save_on(message.from_user.id))
