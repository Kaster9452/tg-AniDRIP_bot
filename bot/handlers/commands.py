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
from bot.handlers.callbacks import admin_label, is_group_admin
from bot.keyboards import main_keyboard, scheduled_keyboard
from bot.publisher import PublishError, notify_scheduled, publish_post
from bot.timeparse import TimeParseError, format_when, parse_when

logger = logging.getLogger(__name__)

router = Router(name="commands")


def build_admin_help(bot_name: str) -> str:
    return f"""🎛 <b>{bot_name}</b> · <i>панель управления</i>

<b>━━━━━ ПРЕДЛОЖКИ ━━━━━</b>
📥 /pending · ждут решения
🕓 /queue · очередь отложенных
✅ /published · последние публикации

<b>━━━━━ ПУБЛИКАЦИЯ ━━━━━</b>
🙈 <code>/post 12</code> · сейчас, анонимно
✍️ <code>/posts 12</code> · сейчас, с подписью автора
🗓 <code>/time 12 завтра 09:00</code> · перенести
🗑 <code>/cancel 12</code> · снять с публикации

<b>━━━━━ СВОЙ ПОСТ ━━━━━</b>
➕ /mypost · написать свой пост и поставить в сетку
Можно сразу с текстом: <code>/mypost Привет, канал</code>
Время выбирается кнопками — по три часа, от 02:00 до 23:00.

<b>━━━━━ ЛЮДИ ━━━━━</b>
🚫 /bans · кто заблокирован
🔓 <code>/unban 943554719</code> · снять блокировку

<b>━━━━━ ПРОЧЕЕ ━━━━━</b>
🗂 /panel · панель модерации целиком
🆔 /id · узнать ID этого чата

<blockquote>💬 <b>Ответ автору</b> — свайпните по его сообщению в группе и напишите ответ, он уйдёт в личку.

🚫 <b>Блокировка</b> — тем же свайпом словом <code>бан</code>, можно с причиной: <code>бан спам</code>. Снять: <code>разбан</code>.

🕐 <b>Форматы времени</b> для <code>/time</code>: <code>18:30</code> · <code>завтра 09:00</code> · <code>25.08 20:00</code> · <code>+2ч</code> · <code>+30м</code> · <code>+1д</code></blockquote>"""


USER_HELP = """👋 <b>Это предложка канала</b>

Присылайте сюда всё, что считаете достойным: текст, фото, видео.
Администраторы посмотрят и, если подойдёт, опубликуют.

Публикуем анонимно или с вашей подписью — решают админы.
Ответить вам они тоже могут прямо здесь."""


def describe(post, tz: ZoneInfo) -> str:
    author = f"@{post['author_username']}" if post["author_username"] else (
        post["author_name"] or "аноним"
    )
    preview = (post["content_html"] or f"[{post['content_type']}]").strip()
    # html_text уже экранирован, обрезаем аккуратно по длине
    if len(preview) > 60:
        preview = preview[:60] + "…"
    return f"<b>#{post['id']}</b> от {author}\n{preview}"


@router.message(Command("help"))
async def cmd_help(
    message: Message, bot: Bot, admin_group_id: int, bot_name: str = ""
) -> None:
    """Админам — полный список команд, остальным — короткая памятка."""
    if await is_group_admin(bot, admin_group_id, message.from_user.id):
        await message.reply(build_admin_help(bot_name or "Предложка"))
    else:
        await message.reply(USER_HELP)


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.reply(f"Chat ID: <code>{message.chat.id}</code>")


@router.message(Command("queue"))
async def cmd_queue(message: Message, bot: Bot, admin_group_id: int, tz: ZoneInfo) -> None:
    if not await is_group_admin(bot, admin_group_id, message.from_user.id):
        await message.reply("Эта команда доступна только администраторам группы.")
        return

    posts = await db.get_scheduled_posts()
    if not posts:
        await message.reply("Очередь пуста — отложенных постов нет.")
        return

    lines = ["<b>🕓 Отложенные посты</b>\n"]
    for post in posts:
        signature = "с подписью" if post["with_attribution"] else "анонимно"
        when = format_when(post["scheduled_at"], tz)
        who = f" · отложил {post['scheduled_by']}" if post["scheduled_by"] else ""
        lines.append(f"{describe(post, tz)}\n→ {when} ({signature}){who}\n")
    await message.reply("\n".join(lines))


@router.message(Command("pending"))
async def cmd_pending(message: Message, bot: Bot, admin_group_id: int, tz: ZoneInfo) -> None:
    if not await is_group_admin(bot, admin_group_id, message.from_user.id):
        await message.reply("Эта команда доступна только администраторам группы.")
        return

    posts = await db.get_pending_posts()
    if not posts:
        await message.reply("Нет предложек, ждущих решения.")
        return

    lines = ["<b>📨 Ждут решения</b>\n"]
    for post in posts:
        lines.append(describe(post, tz) + "\n")
    await message.reply("\n".join(lines))


@router.message(Command("published"))
async def cmd_published(message: Message, bot: Bot, admin_group_id: int, tz: ZoneInfo) -> None:
    if not await is_group_admin(bot, admin_group_id, message.from_user.id):
        await message.reply("Эта команда доступна только администраторам группы.")
        return

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
async def cmd_cancel(message: Message, command: CommandObject, bot: Bot, admin_group_id: int) -> None:
    if not await is_group_admin(bot, admin_group_id, message.from_user.id):
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
    message: Message, command: CommandObject, bot: Bot, admin_group_id: int, tz: ZoneInfo
) -> None:
    if not await is_group_admin(bot, admin_group_id, message.from_user.id):
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
        post_id,
        when.astimezone(timezone.utc),
        post["with_attribution"],
        admin_label(message.from_user),
    )
    await notify_scheduled(bot, post, when, tz)
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
    message: Message, command: CommandObject, bot: Bot, channel_id: int, admin_group_id: int, signed: bool
) -> None:
    if not await is_group_admin(bot, admin_group_id, message.from_user.id):
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
    message: Message, command: CommandObject, bot: Bot, channel_id: int, admin_group_id: int
) -> None:
    await _publish_now(message, command, bot, channel_id, admin_group_id, signed=False)


@router.message(Command("posts"))
async def cmd_post_signed(
    message: Message, command: CommandObject, bot: Bot, channel_id: int, admin_group_id: int
) -> None:
    await _publish_now(message, command, bot, channel_id, admin_group_id, signed=True)

@router.message(Command("panel"))
async def cmd_panel(
    message: Message,
    bot: Bot,
    webapp_url: str,
    webapp_short_name: str,
    bot_username: str,
    admin_group_id: int,
) -> None:
    """Открывает панель модерации.

    В личке используется обычная кнопка Mini App. В группах Telegram такие
    кнопки запрещает, поэтому там даём прямую ссылку вида
    t.me/бот/имя_приложения — она открывает то же самое приложение
    и работает в любом чате.
    """
    if not await is_group_admin(bot, admin_group_id, message.from_user.id):
        await message.reply("Панель доступна только администраторам группы.")
        return

    if not webapp_url:
        await message.reply(
            "Панель не настроена: не задан адрес WEBAPP_URL в переменных окружения."
        )
        return

    if message.chat.type == "private":
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
        return

    # группа или супергруппа
    if not webapp_short_name or not bot_username:
        await message.reply(
            "Чтобы открывать панель прямо из группы, нужно один раз "
            "зарегистрировать приложение:\n\n"
            "1. Напишите @BotFather команду /newapp и выберите этого бота\n"
            f"2. Укажите адрес <code>{webapp_url}/app</code>\n"
            "3. Придумайте короткое имя, например <code>panel</code>\n"
            "4. Добавьте его в переменную <code>WEBAPP_SHORT_NAME</code> на Render\n\n"
            "А пока панель открывается командой /panel в личке со мной."
        )
        return

    link = f"https://t.me/{bot_username}/{webapp_short_name}"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🗂 Открыть панель", url=link)]]
    )
    await message.reply(
        "Панель модерации: очередь, публикация и расписание в одном окне.",
        reply_markup=keyboard,
    )


@router.message(Command("bans"))
async def cmd_bans(message: Message, bot: Bot, admin_group_id: int) -> None:
    if not await is_group_admin(bot, admin_group_id, message.from_user.id):
        await message.reply("Эта команда доступна только администраторам группы.")
        return

    rows = await db.get_banned_users()
    if not rows:
        await message.reply("Заблокированных нет.")
        return

    lines = ["<b>🚫 Заблокированные</b>\n"]
    for row in rows:
        who = f"@{row['username']}" if row["username"] else (row["name"] or "без имени")
        line = f"{who} — <code>{row['user_id']}</code>"
        if row["reason"]:
            line += f"\nпричина: {row['reason']}"
        lines.append(line + "\n")
    lines.append("Снять: <code>/unban ID</code> или ответом «разбан» на сообщение.")
    await message.reply("\n".join(lines))


@router.message(Command("unban"))
async def cmd_unban(
    message: Message, command: CommandObject, bot: Bot, admin_group_id: int
) -> None:
    if not await is_group_admin(bot, admin_group_id, message.from_user.id):
        await message.reply("Эта команда доступна только администраторам группы.")
        return

    if not command.args:
        await message.reply("Укажите ID, например: <code>/unban 943554719</code>")
        return

    try:
        user_id = int(command.args.split()[0])
    except ValueError:
        await message.reply("ID должен быть числом. Посмотреть можно в /bans.")
        return

    if await db.unban_user(user_id):
        await message.reply("✅ Блокировка снята.")
    else:
        await message.reply("Этот пользователь не был заблокирован.")
