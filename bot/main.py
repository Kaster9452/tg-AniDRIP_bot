import asyncio
import logging

from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.config import load_config
from bot.database import init_db
from bot.handlers import admin, common, user


async def handle_ping(request: web.Request) -> web.Response:
    """Отвечает на любой GET-запрос. Нужен для двух вещей:
    1) Render считает Web Service живым, только если он слушает порт;
    2) внешний будильник (например, cron-job.org) стучится сюда каждые
       несколько минут, чтобы Render не усыпил сервис из-за простоя."""
    return web.Response(text="Bot is running")


async def start_web_server(port: int) -> None:
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logging.info("Веб-сервер для будильника запущен на порту %s", port)


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

    await asyncio.gather(
        dp.start_polling(bot, admin_group_id=config.admin_group_id),
        start_web_server(config.port),
    )


if __name__ == "__main__":
    asyncio.run(main())
