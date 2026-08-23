import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message

from bot import database as db

logger = logging.getLogger(__name__)

router = Router(name="admin")

# Первое слово ответа, которое означает команду, а не текст для пользователя
BAN_WORDS = {"бан", "ban", "блок", "забанить"}
UNBAN_WORDS = {"разбан", "unban", "разблок", "разбанить"}


def parse_command(text: str) -> tuple[str | None, str | None]:
    """Разбирает ответ админа: команда это или обычное сообщение.

    Возвращает пару (действие, причина). Если это не команда — (None, None).
    """
    stripped = (text or "").strip()
    if not stripped:
        return None, None

    parts = stripped.split(maxsplit=1)
    first = parts[0].lower().strip(".,!:;").replace("ё", "е")
    rest = parts[1].strip() if len(parts) > 1 else None

    if first in BAN_WORDS:
        return "ban", rest
    if first in UNBAN_WORDS:
        return "unban", rest
    return None, None


def describe(message: Message) -> str:
    user = message.from_user
    return f"@{user.username}" if user.username else user.full_name


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_admin_reply(message: Message, bot: Bot, admin_group_id: int) -> None:
    if message.chat.id != admin_group_id:
        return  # сообщение из какой-то другой группы — не наше дело

    if not message.reply_to_message:
        return  # обычное сообщение админов между собой, не ответ юзеру

    target_user_id = await db.get_user_id(message.reply_to_message.message_id)
    if target_user_id is None:
        return  # реплай не на пересланное ботом сообщение

    action, reason = parse_command(message.text or message.caption or "")

    if action == "ban":
        await do_ban(message, target_user_id, reason)
        return

    if action == "unban":
        await do_unban(message, target_user_id)
        return

    try:
        await message.copy_to(chat_id=target_user_id)
    except TelegramForbiddenError:
        await message.reply("⚠️ Пользователь заблокировал бота, ответ не доставлен.")
    except TelegramBadRequest:
        logger.exception("Не удалось отправить ответ пользователю %s", target_user_id)
        await message.reply("⚠️ Не удалось отправить сообщение.")


async def do_ban(message: Message, target_user_id: int, reason: str | None) -> None:
    post = await db.get_post_by_user_message(target_user_id)
    username = post["author_username"] if post else None
    name = post["author_name"] if post else None

    await db.ban_user(target_user_id, username, name, reason, describe(message))
    dropped = await db.reject_pending_by_user(target_user_id)

    who = f"@{username}" if username else (name or f"ID {target_user_id}")
    lines = [f"🚫 {who} заблокирован — его сообщения больше не будут приходить."]
    if reason:
        lines.append(f"Причина: {reason}")
    if dropped:
        lines.append(f"Из очереди убрано постов: {dropped}.")
    lines.append("Снять блокировку: ответьте на это сообщение словом «разбан».")
    await message.reply("\n".join(lines))


async def do_unban(message: Message, target_user_id: int) -> None:
    was_banned = await db.unban_user(target_user_id)
    if was_banned:
        await message.reply("✅ Блокировка снята, человек снова может писать боту.")
    else:
        await message.reply("Этот пользователь и так не был заблокирован.")
