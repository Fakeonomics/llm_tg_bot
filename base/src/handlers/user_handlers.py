from aiogram import Bot, F, Router
from aiogram.types import Message

from src.errors.errors import EmptyTrancriptionResult
from src.handlers.helpers import debug_handler_reply
from src.models import TelegramDialogManager, DictDialogStorage
from src.services import access as access_ctl
from src.services import moderation as mod
from src.services import savectl
from src.services.agent_loop import deliver
from src.services.messages import SystemMessage, get_message


router = Router()
dialog_manager = TelegramDialogManager(DictDialogStorage())


@router.message(F.content_type == "text", F.chat.type == "private", ~F.text.startswith("/"))
@debug_handler_reply
async def process_text_message(message: Message):
    """Private text -> smolagents agent (acts, no manual commands)."""
    uid = message.from_user.id
    access_ctl.bootstrap_admin(uid)
    if not access_ctl.is_allowed(uid):
        return
    text = message.text or ""
    if not text:
        await message.reply(text=get_message(SystemMessage.NO_INPUT))
        return
    admin_id = access_ctl.get_admin_id()
    blocked, _ = mod.check(text, uid, admin_id)
    if blocked:
        await message.reply(mod.REFUSE_RU)
        return
    await deliver(message, text, admin_id, savectl.is_save_on(uid))


@router.message(F.content_type == "voice")
@debug_handler_reply
async def process_voice_message(message: Message, bot: Bot):
    """Gets audio update and sends answer of an Open AI chatbot model."""
    try:
        await dialog_manager.reply_on_voice(message, bot)

    except EmptyTrancriptionResult:
        answer = get_message(SystemMessage.UNINTELLIGIBLE_VOICE_INPUT)
        await message.reply(text=answer)
