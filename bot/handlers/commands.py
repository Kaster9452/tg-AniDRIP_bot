import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from bot import database as db
from bot.handlers.callbacks import is_group_admin
from bot.keyboards import main_keyboard, scheduled_keyboard
from bot.publisher import PublishError, publish_post
from bot.timeparse import TimeParseError, format_when, parse_when

logger = logging.getLogger(__name__)

router = Router(name="commands")

HELP_TEXT = """<b>Команды бота</b>

<b>В группе админов:</b>
/queue — очередь отложенных постов
/pending — предложки, ждущие решения
/published — последние опубликованные
/cancel <code>[номер]</code> — отменить отложенную публикацию
/time <code>[номер] [когда]</code> — перенести на другое время
/post <code>[номер]</code> — опубликовать прямо сейчас (анонимно)
/posts <code>[номер]</code> — опубликовать прямо сейчас (с подписью)
/panel — открыть панель модерации (в личке с ботом)
/id — узнать ID текущего чата
/help — эта справка

<b>Форматы времени:</b>
<code>18:30</code> · <code>завтра 09:00</code> · <code>25.08 20:00</code> · <code>25.08.2026 20:00</code> · <code>+2ч</code> · <code>+30м</code> · <code>+1д</code>

Отвечать пользователю можно свайпом (Reply) по его сообщению в группе."""


def describe(post, tz: ZoneInfo) -> str:
    author = f"@{post['author_username']}" if post["author_username"] else (
        post["author_name"] or "аноним"
    )
    preview = (post["content_html"] or f"[{post['content_type']}]").strip()
    # html_text уже экранирован, обрезаем аккуратно по длине
    if len(preview) > 60:
        preview = preview[:60] + "…"
    return f"<b>#{post['id']}</b> от {author}\n{preview}"


@router.message(Command("help", "start"))
async def cmd_help(message: Message) -> None:
    if message.chat.type in ("group", "supergroup"):
        await message.reply(HELP_TEXT)


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.reply(f"Chat ID: <code>{message.chat.id}</code>")


@router.message(Command("queue"))
async def cmd_queue(message: Message, tz: ZoneInfo) -> None:
    posts = await db.get_scheduled_posts()
    if not posts:
        await message.reply("Очередь пуста — отложенных постов нет.")
        return

    lines = ["<b>🕓 Отложенные посты</b>\n"]
    for post in posts:
        signature = "с подписью" if post["with_attribution"] else "анонимно"
        when = format_when(post["scheduled_at"], tz)
        lines.append(f"{describe(post, tz)}\n→ {when} ({signature})\n")
    await message.reply("\n".join(lines))


@router.message(Command("pending"))
async def cmd_pending(message: Message, tz: ZoneInfo) -> None:
    posts = await db.get_pending_posts()
    if not posts:
        await message.reply("Нет предложек, ждущих решения.")
        return

    lines = ["<b>📨 Ждут решения</b>\n"]
    for post in posts:
        lines.append(describe(post, tz) + "\n")
    await message.reply("\n".join(lines))


@router.message(Command("published"))
async def cmd_published(message: Message, tz: ZoneInfo) -> None:
    posts = await db.get_published_posts()
    if not posts:
        await message.reply("Пока ничего не опубликовано.")
        return

    lines = ["<b>✅ Последние публикации</b>\n"]
    for post in posts:
        when = format_when(post["published_at"], tz)
        lines.append(f"{describe(post, tz)}\n→ опубликован {when}\n")
    await message.reply("\n".join(lines))


def parse_post_id(command: CommandObject) -> int | None:
    if not command.args:
        return None
    first = command.args.split()[0].lstrip("#")
    try:
        return int(first)
    except ValueError:
        return None


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, command: CommandObject, bot: Bot) -> None:
    if not await is_group_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("Эта команда доступна только администраторам группы.")
        return

    post_id = parse_post_id(command)
    if post_id is None:
        await message.reply("Укажите номер поста, например: <code>/cancel 12</code>")
        return

    post = await db.get_post(post_id)
    if post is None:
        await message.reply(f"Пост #{post_id} не найден.")
        return
    if post["status"] != "scheduled":
        await message.reply(f"Пост #{post_id} не запланирован (статус: {post['status']}).")
        return

    await db.mark_cancelled(post_id)
    if post["admin_chat_id"] and post["admin_message_id"]:
        try:
            await bot.edit_message_reply_markup(
                chat_id=post["admin_chat_id"],
                message_id=post["admin_message_id"],
                reply_markup=main_keyboard(post_id),
            )
        except Exception:
            logger.debug("Не удалось обновить кнопки у поста %s", post_id)

    await message.reply(f"🗑 Публикация поста #{post_id} отменена.")


@router.message(Command("time"))
async def cmd_reschedule(
    message: Message, command: CommandObject, bot: Bot, tz: ZoneInfo
) -> None:
    if not await is_group_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("Эта команда доступна только администраторам группы.")
        return

    if not command.args or len(command.args.split()) < 2:
        await message.reply(
            "Укажите номер и время, например: <code>/time 12 завтра 09:00</code>"
        )
        return

    parts = command.args.split(maxsplit=1)
    try:
        post_id = int(parts[0].lstrip("#"))
    except ValueError:
        await message.reply("Первым аргументом должен быть номер поста.")
        return

    post = await db.get_post(post_id)
    if post is None:
        await message.reply(f"Пост #{post_id} не найден.")
        return
    if post["status"] == "published":
        await message.reply(f"Пост #{post_id} уже опубликован.")
        return

    try:
        when = parse_when(parts[1], tz)
    except TimeParseError as exc:
        await message.reply(f"⚠️ {exc}")
        return

    if when <= datetime.now(tz):
        await message.reply("⚠️ Это время уже прошло.")
        return

    await db.mark_scheduled(
        post_id, when.astimezone(timezone.utc), post["with_attribution"]
    )
    if post["admin_chat_id"] and post["admin_message_id"]:
        try:
            await bot.edit_message_reply_markup(
                chat_id=post["admin_chat_id"],
                message_id=post["admin_message_id"],
                reply_markup=scheduled_keyboard(post_id),
            )
        except Exception:
            logger.debug("Не удалось обновить кнопки у поста %s", post_id)

    await message.reply(f"🕓 Пост #{post_id} перенесён на <b>{format_when(when, tz)}</b>.")


async def _publish_now(
    message: Message, command: CommandObject, bot: Bot, channel_id: int, signed: bool
) -> None:
    if not await is_group_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("Эта команда доступна только администраторам группы.")
        return

    post_id = parse_post_id(command)
    if post_id is None:
        await message.reply("Укажите номер поста, например: <code>/post 12</code>")
        return

    post = await db.get_post(post_id)
    if post is None:
        await message.reply(f"Пост #{post_id} не найден.")
        return
    if post["status"] == "published":
        await message.reply(f"Пост #{post_id} уже опубликован.")
        return

    try:
        await publish_post(bot, post, channel_id, signed)
    except PublishError as exc:
        await message.reply(f"⚠️ Ошибка публикации поста #{post_id}: {exc}")
        return

    if post["admin_chat_id"] and post["admin_message_id"]:
        try:
            await bot.edit_message_reply_markup(
                chat_id=post["admin_chat_id"],
                message_id=post["admin_message_id"],
                reply_markup=None,
            )
        except Exception:
            logger.debug("Не удалось убрать кнопки у поста %s", post_id)

    signature = "с подписью" if signed else "анонимно"
    await message.reply(f"✅ Пост #{post_id} опубликован ({signature}).")


@router.message(Command("post"))
async def cmd_post_anon(
    message: Message, command: CommandObject, bot: Bot, channel_id: int
) -> None:
    await _publish_now(message, command, bot, channel_id, signed=False)


@router.message(Command("posts"))
async def cmd_post_signed(
    message: Message, command: CommandObject, bot: Bot, channel_id: int
) -> None:
    await _publish_now(message, command, bot, channel_id, signed=True)

@router.message(Command("panel"))
async def cmd_panel(
    message: Message, bot: Bot, webapp_url: str, admin_group_id: int
) -> None:
    """Кнопка запуска Mini App. Telegram разрешает такие кнопки только
    в личной переписке с ботом, в группах они не работают."""
    if message.chat.type != "private":
        await message.reply("Откройте панель в личном чате со мной: команда /panel")
        return

    if not webapp_url:
        await message.answer(
            "Панель не настроена: не задан адрес WEBAPP_URL в переменных окружения."
        )
        return

    if not await is_group_admin(bot, admin_group_id, message.from_user.id):
        await message.answer("Панель доступна только администраторам группы.")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗂 Открыть панель",
                    web_app=WebAppInfo(url=f"{webapp_url}/app"),
                )
            ]
        ]
    )
    await message.answer(
        "Панель модерации: очередь, публикация и расписание в одном окне.",
        reply_markup=keyboard,
    )
