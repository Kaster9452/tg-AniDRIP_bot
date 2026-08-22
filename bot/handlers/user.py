import logging

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from bot.database import save_mapping

logger = logging.getLogger(__name__)

router = Router(name="user")

# Типы контента, у которых в Telegram есть поле caption
CAPTIONABLE_TYPES = {"photo", "video", "document", "audio", "animation", "voice"}


def build_header(message: Message) -> str:
    user = message.from_user
    username = f"@{user.username}" if user.username else "нет username"
    return f"👤 {user.full_name} ({username})\nID: <code>{user.id}</code>"


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Напиши сюда сообщение — оно будет передано администраторам, "
        "и они ответят тебе прямо в этом чате."
    )


@router.message(F.chat.type == "private")
async def forward_to_admins(message: Message, bot: Bot, admin_group_id: int) -> None:
    header = build_header(message)

    try:
        if message.content_type == "text":
            sent = await bot.send_message(admin_group_id, f"{header}\n\n{message.text}")
            await save_mapping(sent.message_id, message.from_user.id, message.message_id)

        elif message.content_type in CAPTIONABLE_TYPES:
            caption = header
            if message.caption:
                caption += f"\n\n{message.caption}"
            sent = await message.copy_to(chat_id=admin_group_id, caption=caption)
            await save_mapping(sent.message_id, message.from_user.id, message.message_id)

        else:
            # Стикеры, голосовые заметки, геолокация и т.п. — caption не поддерживают,
            # поэтому сначала шлём отдельным сообщением инфо о юзере, потом сам контент.
            # Обе связки ведут на одного и того же пользователя — ответить можно на любую.
            info_msg = await bot.send_message(admin_group_id, header)
            await save_mapping(info_msg.message_id, message.from_user.id, message.message_id)

            copied = await message.copy_to(chat_id=admin_group_id)
            await save_mapping(copied.message_id, message.from_user.id, message.message_id)

    except Exception:
        logger.exception("Не удалось переслать сообщение от %s", message.from_user.id)
        await message.answer("⚠️ Не получилось отправить сообщение, попробуйте позже.")
        return

    await message.answer("✅ Сообщение отправлено администраторам.")
