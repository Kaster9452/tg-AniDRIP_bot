import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeChatAdministrators,
    BotCommandScopeDefault,
    ErrorEvent,
    MenuButtonDefault,
    MenuButtonWebApp,
    WebAppInfo,
)

from bot.config import load_config
from bot.database import close_db, init_db
from bot.handlers import admin, callbacks, commands, ownpost, user
from bot.scheduler import run_scheduler
from bot.webapp import start_web_server

# Меню команд в Telegram (подсказка при вводе "/") — только для админов,
# обычные пользователи в личке её не видят.
ADMIN_COMMANDS = [
    BotCommand(command="pending", description="Ждут решения"),
    BotCommand(command="queue", description="Очередь отложенных"),
    BotCommand(command="published", description="Последние публикации"),
    BotCommand(command="post", description="Опубликовать сейчас, анонимно"),
    BotCommand(command="posts", description="Опубликовать сейчас, с подписью"),
    BotCommand(command="time", description="Перенести время публикации"),
    BotCommand(command="cancel", description="Снять с публикации"),
    BotCommand(command="mypost", description="Написать свой пост"),
    BotCommand(command="bans", description="Список заблокированных"),
    BotCommand(command="unban", description="Снять блокировку"),
    BotCommand(command="panel", description="Панель модерации"),
    BotCommand(command="id", description="ID этого чата"),
    BotCommand(command="help", description="Справка"),
]


async def _setup_bot_commands(bot: Bot, admin_group_id: int) -> None:
    """Регистрирует меню команд так, чтобы его видели только админы:
    в группе админов и у каждого из них лично с ботом. У остальных пустой
    список команд — Telegram сам скрывает кнопку меню, если показывать нечего.
    """
    await bot.set_my_commands([], scope=BotCommandScopeDefault())
    await bot.set_my_commands(
        ADMIN_COMMANDS, scope=BotCommandScopeChatAdministrators(chat_id=admin_group_id)
    )

    try:
        admins = await bot.get_chat_administrators(admin_group_id)
    except Exception:
        logging.exception("Не удалось получить список админов для меню команд")
        return

    for member in admins:
        if member.user.is_bot:
            continue
        try:
            await bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=member.user.id)
            )
        except Exception:
            # Частая причина — админ никогда не писал боту в личку, Telegram не даёт так поставить меню.
            logging.debug("Не удалось поставить меню команд для %s", member.user.id)


async def _setup_menu_button(bot: Bot, admin_group_id: int, webapp_url: str) -> None:
    """Кнопка меню слева от поля ввода. Раньше она (заданная через BotFather)
    открывала панель модерации вообще всем подряд — теперь по умолчанию она
    ничего не открывает, а на панель ведёт только у админов лично в чате с ботом.
    """
    await bot.set_chat_menu_button(menu_button=MenuButtonDefault())

    if not webapp_url:
        return

    admin_button = MenuButtonWebApp(
        text="Панель", web_app=WebAppInfo(url=f"{webapp_url}/app")
    )

    try:
        admins = await bot.get_chat_administrators(admin_group_id)
    except Exception:
        logging.exception("Не удалось получить список админов для кнопки меню")
        return

    for member in admins:
        if member.user.is_bot:
            continue
        try:
            await bot.set_chat_menu_button(chat_id=member.user.id, menu_button=admin_button)
        except Exception:
            logging.debug("Не удалось поставить кнопку меню для %s", member.user.id)


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
    dp.include_router(ownpost.router)
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

    # Имя бота нужно для прямой ссылки на панель вида t.me/бот/имя.
    me = await bot.get_me()
    logging.info("Бот: @%s (id=%s)", me.username, me.id)

    try:
        await _setup_bot_commands(bot, config.admin_group_id)
    except Exception:
        logging.exception("Не удалось настроить меню команд")

    try:
        await _setup_menu_button(bot, config.admin_group_id, config.webapp_url)
    except Exception:
        logging.exception("Не удалось настроить кнопку меню")

    # Эти значения aiogram передаст в обработчики как аргументы
    context = {
        "admin_group_id": config.admin_group_id,
        "channel_id": config.channel_id,
        "tz": config.timezone,
        "webapp_url": config.webapp_url,
        "webapp_short_name": config.webapp_short_name,
        "bot_username": me.username or "",
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
