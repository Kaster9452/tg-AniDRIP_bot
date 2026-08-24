import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from bot import database as db
from bot.keyboards import main_keyboard
from bot.publisher import build_input_media

logger = logging.getLogger(__name__)

router = Router(name="user")

# Типы контента, у которых в Telegram есть поле caption
CAPTIONABLE_TYPES = {"photo", "video", "document", "audio", "animation", "voice"}

# Альбомы приходят несколькими отдельными сообщениями с одинаковым
# media_group_id — собираем их немного и только потом обрабатываем целиком.
ALBUM_WAIT_SECONDS = 1.2
_album_buffers: dict[str, list[Message]] = {}


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
    # Альбом (несколько фото/видео одним сообщением) приходит несколькими
    # отдельными update с одинаковым media_group_id — собираем их отдельно.
    if message.media_group_id:
        await _buffer_album_message(message, bot, admin_group_id)
        return

    user = message.from_user

    # Заблокированные не проходят дальше: админы их сообщений не увидят.
    if await db.is_banned(user.id):
        await message.answer(
            "Отправка предложений для вас закрыта администраторами."
        )
        return

    content_type = message.content_type

    # Голый текст без медиа предложкой не считаем — это может быть просто
    # сообщение админам (вопрос, жалоба и т.п.), а не предложение для канала.
    if content_type == "text":
        await _forward_plain_text(message, bot, admin_group_id, user)
        return

    # Текст или подпись сохраняем в HTML, чтобы не потерять форматирование
    # (жирный, ссылки и т.п.) при последующей публикации в канал.
    content_html = message.html_text if (message.text or message.caption) else None

    # Сохраняем ID медиа, чтобы панель могла показать настоящий файл.
    file_id, media_thumb_id = _extract_media(message, content_type)

    post_id = await db.create_post(
        user_id=user.id,
        user_message_id=message.message_id,
        author_name=user.full_name,
        author_username=user.username,
        content_type=content_type,
        content_html=content_html,
        file_id=file_id,
        media_thumb_id=media_thumb_id,
    )

    header = build_header(message, post_id)
    keyboard = main_keyboard(post_id)

    try:
        if content_type in CAPTIONABLE_TYPES:
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


async def _forward_plain_text(message: Message, bot: Bot, admin_group_id: int, user) -> None:
    """Просто текст без медиа: пересылаем админам как обычное сообщение,
    без создания поста в предложке и без кнопок публикации."""
    username = f"@{user.username}" if user.username else "нет username"
    header = f"💬 {user.full_name} ({username})\nID: <code>{user.id}</code>"

    try:
        sent = await bot.send_message(admin_group_id, f"{header}\n\n{message.text}")
        await db.save_mapping(sent.message_id, user.id, message.message_id)
    except Exception:
        logger.exception("Не удалось переслать сообщение от %s", user.id)
        await message.answer("⚠️ Не получилось отправить сообщение, попробуйте позже.")
        return

    await message.answer("✅ Сообщение отправлено администраторам.")


def _extract_media(message: Message, content_type: str) -> tuple[str | None, str | None]:
    media = message.photo[-1] if message.photo else getattr(message, content_type, None)
    file_id = getattr(media, "file_id", None)
    thumbnail = getattr(media, "thumbnail", None) or getattr(media, "thumb", None)
    media_thumb_id = getattr(thumbnail, "file_id", None)
    return file_id, media_thumb_id


async def _buffer_album_message(message: Message, bot: Bot, admin_group_id: int) -> None:
    group_id = message.media_group_id
    is_first = group_id not in _album_buffers
    _album_buffers.setdefault(group_id, []).append(message)
    if is_first:
        asyncio.create_task(_flush_album(group_id, bot, admin_group_id))


async def _flush_album(group_id: str, bot: Bot, admin_group_id: int) -> None:
    await asyncio.sleep(ALBUM_WAIT_SECONDS)
    messages = _album_buffers.pop(group_id, [])
    if not messages:
        return
    messages.sort(key=lambda m: m.message_id)
    await _process_album(messages, bot, admin_group_id)


async def _process_album(
    messages: list[Message], bot: Bot, admin_group_id: int
) -> None:
    first = messages[0]
    user = first.from_user

    if await db.is_banned(user.id):
        await first.answer("Отправка предложений для вас закрыта администраторами.")
        return

    items: list[dict] = []
    for message in messages:
        file_id, thumb_id = _extract_media(message, message.content_type)
        if not file_id:
            continue
        items.append({"type": message.content_type, "file_id": file_id, "thumb": thumb_id})

    if not items:
        return

    # \u041f\u043e\u0434\u043f\u0438\u0441\u044c \u043a \u0430\u043b\u044c\u0431\u043e\u043c\u0443 Telegram \u043a\u043b\u0430\u0434\u0451\u0442 \u0442\u043e\u043b\u044c\u043a\u043e \u0432 \u043e\u0434\u043d\u043e \u0438\u0437 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0439.
    caption_source = next((m for m in messages if m.text or m.caption), None)
    content_html = caption_source.html_text if caption_source else None

    post_id = await db.create_post(
        user_id=user.id,
        user_message_id=first.message_id,
        author_name=user.full_name,
        author_username=user.username,
        content_type=items[0]["type"],
        content_html=content_html,
        file_id=items[0]["file_id"],
        media_thumb_id=items[0]["thumb"],
        media_group=items,
    )

    header = build_header(first, post_id) + f"\n🖼 Альбом: {len(items)} шт."

    try:
        # \u0423 \u0430\u043b\u044c\u0431\u043e\u043c\u0430 \u043d\u0435\u043b\u044c\u0437\u044f \u043f\u0440\u0438\u043a\u0440\u0435\u043f\u0438\u0442\u044c \u043a\u043d\u043e\u043f\u043a\u0438 \u2014 Telegram \u0438\u0445 \u043d\u0435 \u043f\u043e\u0434\u0434\u0435\u0440\u0436\u0438\u0432\u0430\u0435\u0442 \u0432 send_media_group,
        # \u043f\u043e\u044d\u0442\u043e\u043c\u0443 \u0448\u0430\u043f\u043a\u0430 \u0441 \u043a\u043d\u043e\u043f\u043a\u0430\u043c\u0438 \u0443\u0445\u043e\u0434\u0438\u0442 \u043e\u0442\u0434\u0435\u043b\u044c\u043d\u044b\u043c \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435\u043c.
        info_msg = await bot.send_message(
            admin_group_id, header, reply_markup=main_keyboard(post_id)
        )
        await db.save_mapping(info_msg.message_id, user.id, first.message_id)
        await db.attach_admin_message(post_id, admin_group_id, info_msg.message_id)

        sent_list = await bot.send_media_group(
            admin_group_id, media=build_input_media(items)
        )
        for sent, message in zip(sent_list, messages):
            await db.save_mapping(sent.message_id, user.id, message.message_id)

    except Exception:
        logger.exception("Не удалось переслать альбом от %s", user.id)
        await first.answer("⚠️ Не получилось отправить сообщение, попробуйте позже.")
        return

    await first.answer(f"✅ Альбом из {len(items)} файлов отправлен на модерацию. Спасибо!")

