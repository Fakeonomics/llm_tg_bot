import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from src.config import configs
from src.handlers import agent_handlers, inline_handlers, user_handlers
from src.handlers.seen_mw import SeenMiddleware
from src.services import llama_probe


logger = logging.getLogger(__name__)


async def main():
    if configs.tg_bot.debug_mode:
        logging_level = logging.DEBUG
    else:
        logging_level = logging.INFO
    logging.basicConfig(
        level=logging_level,
        format="%(filename)s:%(lineno)d #%(levelname)-8s "
        "[%(asctime)s] - %(name)s - %(message)s",
    )
    # Privacy: never dump dialog content to disk logs.
    logging.getLogger("src.models.models").setLevel(logging.WARNING)

    logger.info("Starting bot...")

    bot: Bot = Bot(
        token=configs.tg_bot.token,
        # Plain text by default: model/file/command output often contains
        # <...> which would crash HTML parsing. Formatting is opt-in.
        default=DefaultBotProperties(),
    )
    dp: Dispatcher = Dispatcher()

    dp.include_router(agent_handlers.router)
    dp.include_router(user_handlers.router)
    dp.include_router(inline_handlers.router)
    dp.message.middleware(SeenMiddleware())

    import os
    probe_state = llama_probe.probe_llama(
        os.getenv("LLAMA_BASE_URL", "http://localhost:8080/v1"))
    if probe_state["ok"]:
        from src.services.agent_loop import short_id
        logger.info("model: %s | vision=%s",
                    short_id(probe_state["active_model_id"]),
                    probe_state["has_vision"])
        agent_handlers.refresh_probe()
    else:
        logger.warning("model unreachable: %s", probe_state["error"])

    await bot.delete_webhook(drop_pending_updates=True)
    from aiogram.types import BotCommand
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Начать"),
            BotCommand(command="help", description="Что умею"),
            BotCommand(command="status", description="Состояние"),
            BotCommand(command="info", description="Мощности"),
            BotCommand(command="clear", description="Забыть контекст"),
        ])
    except Exception as e:
        logger.warning("set_my_commands failed (offline?): %s", e)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
