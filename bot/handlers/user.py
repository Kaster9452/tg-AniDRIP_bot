import logging

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from bot import database as db
from bot.keyboards import main_keyboard

logger = logging.getLogger(__name__)

router = Router(name="user")

# Типы контента, у которых в Telegram есть поле caption
CAPTIONABLE_TYPES = {"photo", "video", "document", "audio", "animation", "voice"}


def build_header(message: Message, post_id: int) -> str:
    user = message.from_user
    username = f"@{user.username}" if user.username else "нет username"
    return (
        f"📨 Предложка #{post_id}\n"
        f"👤 {user.full_name} ({username})\n"
        f"ID: <code>{user.id}</code>"
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Присылай сюда свои предложения — текст, фото, видео. "
        "Администраторы посмотрят и, если подойдёт, опубликуют в канале. "
        "Ответить тебе они тоже могут прямо здесь."
    )


@router.message(F.chat.type == "private")
async def forward_to_admins(message: Message, bot: Bot, admin_group_id: int) -> None:
    user = message.from_user

    # Заблокированные не проходят дальше: админы их сообщений не увидят.
    if await db.is_banned(user.id):
        await message.answer(
            "Отправка предложений для вас закрыта администраторами."
        )
        return

    content_type = message.content_type

    # Текст или подпись сохраняем в HTML, чтобы не потерять форматирование
    # (жирный, ссылки и т.п.) при последующей публикации в канал.
    content_html = message.html_text if (message.text or message.caption) else None

    # Сохраняем ID медиа, чтобы панель могла показать настоящий файл.
    media = message.photo[-1] if message.photo else getattr(message, content_type, None)
    file_id = getattr(media, "file_id", None)

    post_id = await db.create_post(
        user_id=user.id,
        user_message_id=message.message_id,
        author_name=user.full_name,
        author_username=user.username,
        content_type=content_type,
        content_html=content_html,
        file_id=file_id,
    )

    header = build_header(message, post_id)
    keyboard = main_keyboard(post_id)

    try:
        if content_type == "text":
            sent = await bot.send_message(
                admin_group_id, f"{header}\n\n{message.text}", reply_markup=keyboard
            )
            await db.save_mapping(sent.message_id, user.id, message.message_id)
            await db.attach_admin_message(post_id, admin_group_id, sent.message_id)

        elif content_type in CAPTIONABLE_TYPES:
            caption = header
            if message.caption:
                caption += f"\n\n{message.caption}"
            sent = await message.copy_to(
                chat_id=admin_group_id, caption=caption, reply_markup=keyboard
            )
            await db.save_mapping(sent.message_id, user.id, message.message_id)
            await db.attach_admin_message(post_id, admin_group_id, sent.message_id)

        else:
            # Стикеры, геолокация и т.п. подпись не поддерживают: сначала
            # шапка с кнопками, следом сам контент. Ответить админ может
            # на любое из двух сообщений.
            info_msg = await bot.send_message(
                admin_group_id, header, reply_markup=keyboard
            )
            await db.save_mapping(info_msg.message_id, user.id, message.message_id)
            await db.attach_admin_message(post_id, admin_group_id, info_msg.message_id)

            copied = await message.copy_to(chat_id=admin_group_id)
            await db.save_mapping(copied.message_id, user.id, message.message_id)

    except Exception:
        logger.exception("Не удалось переслать сообщение от %s", user.id)
        await message.answer("⚠️ Не получилось отправить сообщение, попробуйте позже.")
        return

    await message.answer("✅ Отправлено на модерацию. Спасибо!")
