import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent

from bot.config import load_config
from bot.database import close_db, init_db
from bot.handlers import admin, callbacks, commands, user
from bot.scheduler import run_scheduler
from bot.webapp import start_web_server


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = load_config()
    await init_db(config.database_url)
    logging.info("База данных подключена")

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Порядок важен: конкретные команды и кнопки проверяются раньше
    # общих обработчиков, которые ловят любые сообщения в чате.
    dp.include_router(commands.router)
    dp.include_router(callbacks.router)
    dp.include_router(admin.router)
    dp.include_router(user.router)

    @dp.error()
    async def on_error(event: ErrorEvent) -> None:
        """Без этого падение внутри обработчика проходит незаметно:
        кнопка просто бесконечно «крутится», а причина нигде не видна."""
        logging.exception(
            "Необработанная ошибка: %s", event.exception, exc_info=event.exception
        )

    # Эти значения aiogram передаст в обработчики как аргументы
    context = {
        "admin_group_id": config.admin_group_id,
        "channel_id": config.channel_id,
        "tz": config.timezone,
        "webapp_url": config.webapp_url,
    }

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот запущен, начинаю polling...")

    try:
        await asyncio.gather(
            dp.start_polling(bot, **context),
            start_web_server(bot, config),
            run_scheduler(
                bot, config.channel_id, config.admin_group_id, config.timezone
            ),
        )
    finally:
        await close_db()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
