import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.config import load_config
from bot.database import init_db
from bot.handlers import admin, common, user


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = load_config()
    await init_db()

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # common — самый первый: конкретная команда /id должна проверяться
    # раньше общего фильтра admin-роутера по типу чата.
    dp.include_router(common.router)
    dp.include_router(admin.router)
    dp.include_router(user.router)

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот запущен, начинаю polling...")
    await dp.start_polling(bot, admin_group_id=config.admin_group_id)


if __name__ == "__main__":
    asyncio.run(main())
