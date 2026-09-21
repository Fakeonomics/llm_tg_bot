from __future__ import annotations

import logging
import re

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

import agent
import config
from history import HistoryManager

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Shared state — one HistoryManager for all chats
history = HistoryManager()


def _strip_bot_mention(text: str) -> str:
    return re.sub(
        rf"@{re.escape(config.BOT_USERNAME)}\b",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()


# ---------------------------------------------------------------------------
# Context capture commands (any group member)
# ---------------------------------------------------------------------------

async def cmd_begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start recording messages into the capture buffer."""
    chat_id = update.effective_chat.id
    history.begin_capture(chat_id)
    await update.message.reply_text(
        "Recording started. Messages will be captured until /end.\n"
        "Use /clear to wipe the buffer, /status to check progress."
    )


async def cmd_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pause recording (buffer is kept)."""
    chat_id = update.effective_chat.id
    status = history.get_status(chat_id)
    history.end_capture(chat_id)
    await update.message.reply_text(
        f"Recording paused. {status['buffer_count']} message(s) in buffer.\n"
        "Buffer is preserved — tag me to process it, or /clear to reset."
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wipe the capture buffer and stop recording."""
    chat_id = update.effective_chat.id
    history.clear_capture(chat_id)
    await update.message.reply_text("Buffer cleared. Use /begin to start a new session.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current capture state."""
    chat_id = update.effective_chat.id
    s = history.get_status(chat_id)
    state = "ON" if s["capture_active"] else "OFF"
    await update.message.reply_text(
        f"Capture: {state}\n"
        f"Buffered messages: {s['buffer_count']}\n"
        f"Active model: {config.current_model_name()}"
    )


# ---------------------------------------------------------------------------
# Admin commands
# ---------------------------------------------------------------------------

def _is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_USER_IDS


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch LLM backend/model (admin only)."""
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.message.reply_text("Sorry, only admins can change the model.")
        return

    if not context.args:
        await update.message.reply_text(
            f"Current model: {config.current_model_name()}\n\n"
            "Usage: /model <spec>\n"
            "Examples:\n"
            "  /model gemini-2.0-flash\n"
            "  /model gemini-1.5-pro\n"
            "  /model ollama:gemma4:26b\n"
            "  /model ollama:qwen3-coder-next:latest"
        )
        return

    model_spec = context.args[0].strip()
    try:
        new_name = config.set_active_backend(model_spec)
        await update.message.reply_text(f"Switched to model: {new_name}")
    except Exception as e:
        await update.message.reply_text(f"Failed to switch model: {e}")


# ---------------------------------------------------------------------------
# Main message handler
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all text messages in groups and private chats."""
    message = update.message
    if not message or not message.text:
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    username = user.username or user.first_name or "Unknown"

    # Always record the message if capture is active
    history.record_message(chat_id, username, message.text)

    # Check if the bot is mentioned
    bot_mention = f"@{config.BOT_USERNAME}"
    if bot_mention.lower() not in message.text.lower():
        return

    # Strip the mention to get the actual request
    user_request = _strip_bot_mention(message.text)

    if not user_request:
        await message.reply_text(
            "Hi! Tag me with a question or request after my name.\n"
            "Use /begin to start recording context, then tag me to process it."
        )
        return

    # Check for context
    context_messages = history.get_context_for_agent(chat_id)
    if context_messages is None:
        await message.reply_text(
            "No context captured yet. Use /begin to start recording messages, "
            "then tag me when you're ready."
        )
        return

    context_text = HistoryManager.format_for_llm(context_messages)
    buffered_images = history.get_images_from_buffer(chat_id)

    # Show typing indicator and process
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        reply = await agent.get_response(
            user_request=user_request,
            context_text=context_text,
            chat_id=chat_id,
            history_manager=history,
            images=buffered_images or None,
        )
        await message.reply_text(reply, parse_mode="HTML")
    except Exception as e:
        logger.error("Error handling mention: %s", e, exc_info=True)
        # Fallback without markdown if there's a parsing error
        try:
            await message.reply_text(reply)
        except:
            await message.reply_text(
                "Sorry, something went wrong processing your request. Please try again."
            )


async def handle_group_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Record a group photo; respond if the bot is @mentioned in the caption."""
    message = update.message
    if not message or not message.photo:
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    username = user.username or user.first_name or "Unknown"
    caption = message.caption or ""

    # Always download and store the image so it's available when the bot is tagged later
    photo_file = await context.bot.get_file(message.photo[-1].file_id)
    image_bytes = bytes(await photo_file.download_as_bytearray())
    history.record_photo(chat_id, username, caption, image_bytes=image_bytes)

    bot_mention = f"@{config.BOT_USERNAME}"
    if bot_mention.lower() not in caption.lower():
        return

    user_request = _strip_bot_mention(caption) or "What's in this image?"

    context_messages = history.get_context_for_agent(chat_id)
    if context_messages is None:
        await message.reply_text(
            "No context captured yet. Use /begin to start recording, then tag me."
        )
        return

    context_text = HistoryManager.format_for_llm(context_messages)
    buffered_images = history.get_images_from_buffer(chat_id)

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        reply = await agent.get_response(
            user_request=user_request,
            context_text=context_text,
            chat_id=chat_id,
            history_manager=history,
            images=buffered_images,
        )
        await message.reply_text(reply, parse_mode="HTML")
    except Exception as e:
        logger.error("Error handling group photo: %s", e, exc_info=True)
        try:
            await message.reply_text(reply)
        except:
            await message.reply_text("Sorry, something went wrong. Please try again.")


async def handle_private_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a photo sent directly to the bot."""
    message = update.message
    if not message or not message.photo:
        return

    chat_id = message.chat_id
    user = update.effective_user
    username = user.username or user.first_name or "Unknown"
    caption = message.caption or ""

    photo_file = await context.bot.get_file(message.photo[-1].file_id)
    image_bytes = bytes(await photo_file.download_as_bytearray())
    history.record_photo(chat_id, username, caption, image_bytes=image_bytes)

    context_messages = history.get_context_for_agent(chat_id)
    context_text = HistoryManager.format_for_llm(context_messages) if context_messages else "(No captured context)"
    buffered_images = history.get_images_from_buffer(chat_id)
    if not buffered_images:
        buffered_images = [image_bytes]
    user_request = caption or "What's in this image?"

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        reply = await agent.get_response(
            user_request=user_request,
            context_text=context_text,
            chat_id=chat_id,
            history_manager=history,
            images=buffered_images,
        )
        await message.reply_text(reply, parse_mode="HTML")
    except Exception as e:
        logger.error("Error handling private photo: %s", e, exc_info=True)
        try:
            await message.reply_text(reply)
        except:
            await message.reply_text("Sorry, something went wrong. Please try again.")


async def handle_private_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle direct messages to the bot."""
    message = update.message
    if not message or not message.text:
        return

    chat_id = message.chat_id
    user = update.effective_user
    username = user.username or user.first_name or "Unknown"

    # Record into capture buffer if active
    history.record_message(chat_id, username, message.text)

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    context_messages = history.get_context_for_agent(chat_id)
    if context_messages:
        context_text = HistoryManager.format_for_llm(context_messages)
    else:
        context_text = "(No captured context — use /begin to start recording)"
    buffered_images = history.get_images_from_buffer(chat_id)

    try:
        reply = await agent.get_response(
            user_request=message.text,
            context_text=context_text,
            chat_id=chat_id,
            history_manager=history,
            images=buffered_images or None,
        )
        await message.reply_text(reply, parse_mode="HTML")
    except Exception as e:
        logger.error("Error handling DM: %s", e, exc_info=True)
        try:
            await message.reply_text(reply)
        except:
            await message.reply_text("Sorry, something went wrong. Please try again.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    # Always start with local Ollama; Gemini is used only as runtime fallback.
    model_spec = f"ollama:{config.OLLAMA_MODEL}"

    try:
        active = config.set_active_backend(model_spec)
        logger.info("LLM backend initialised: %s", active)
    except Exception as e:
        logger.error("Failed to initialise LLM backend: %s", e)
        raise

    # Bypass proxy env vars (ALL_PROXY etc.) — connect directly to Telegram API.
    request = HTTPXRequest(httpx_kwargs={"trust_env": False})
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).request(request).build()

    # Context capture commands (all users)
    app.add_handler(CommandHandler("begin", cmd_begin))
    app.add_handler(CommandHandler("end", cmd_end))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("status", cmd_status))

    # Admin commands
    app.add_handler(CommandHandler("model", cmd_model))

    # Group message handler (all text that isn't a command)
    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
            handle_message,
        )
    )

    # Group photo handler
    app.add_handler(
        MessageHandler(
            filters.PHOTO & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
            handle_group_photo,
        )
    )

    # Private message handler
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            handle_private_message,
        )
    )

    # Private photo handler
    app.add_handler(
        MessageHandler(
            filters.PHOTO & filters.ChatType.PRIVATE,
            handle_private_photo,
        )
    )

    logger.info("Bot @%s starting (polling)...", config.BOT_USERNAME)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
