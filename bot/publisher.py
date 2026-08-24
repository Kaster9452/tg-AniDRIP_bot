"""Публикация предложенного поста в канал.

Контент копируется из личного чата с автором, а не из группы админов:
в группе к сообщению приклеена шапка с данными отправителя, и она не
должна попасть в канал.
"""

from __future__ import annotations

import json
import logging

import asyncpg
from aiogram import Bot
from aiogram.types import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)

from bot import database as db

logger = logging.getLogger(__name__)

# Типы контента, которым Telegram позволяет задать подпись
CAPTIONABLE_TYPES = {"photo", "video", "document", "audio", "animation", "voice"}

_INPUT_MEDIA_TYPES = {
    "photo": InputMediaPhoto,
    "video": InputMediaVideo,
    "document": InputMediaDocument,
    "audio": InputMediaAudio,
}


class PublishError(RuntimeError):
    """Не удалось опубликовать пост в канал."""


def build_input_media(items: list[dict], caption: str | None = None) -> list:
    """Собирает альбом для send_media_group.

    Telegram показывает подпись только у первого элемента альбома —
    остальным её ставить не нужно, иначе она продублируется под каждым.
    """
    media = []
    for index, item in enumerate(items):
        cls = _INPUT_MEDIA_TYPES.get(item["type"], InputMediaPhoto)
        media.append(cls(media=item["file_id"], caption=caption if index == 0 else None))
    return media


def attribution_line(post: asyncpg.Record) -> str:
    if post["author_username"]:
        return f"Прислал: @{post['author_username']}"
    name = post["author_name"] or "аноним"
    return f'Прислал: <a href="tg://user?id={post["user_id"]}">{name}</a>'


async def publish_post(
    bot: Bot, post: asyncpg.Record, channel_id: int, with_attribution: bool
) -> int:
    """Отправляет пост в канал и возвращает ID сообщения в канале."""
    content_type = post["content_type"]
    body = post["content_html"] or ""
    suffix = f"\n\n{attribution_line(post)}" if with_attribution else ""

    try:
        if post["media_group"]:
            items = json.loads(post["media_group"])
            caption = f"{body}{suffix}".strip() or None
            sent_list = await bot.send_media_group(
                channel_id, media=build_input_media(items, caption)
            )
            sent = sent_list[0]

        elif content_type == "text":
            sent = await bot.send_message(channel_id, f"{body}{suffix}")

        elif content_type in CAPTIONABLE_TYPES:
            caption = f"{body}{suffix}".strip() or None
            sent = await bot.copy_message(
                chat_id=channel_id,
                from_chat_id=post["user_id"],
                message_id=post["user_message_id"],
                caption=caption,
            )

        else:
            # Стикеры, геолокация и подобное подпись не поддерживают —
            # копируем как есть, а автора при необходимости шлём следом.
            sent = await bot.copy_message(
                chat_id=channel_id,
                from_chat_id=post["user_id"],
                message_id=post["user_message_id"],
            )
            if with_attribution:
                await bot.send_message(channel_id, attribution_line(post))

    except Exception as exc:
        logger.exception("Не удалось опубликовать пост #%s", post["id"])
        raise PublishError(str(exc)) from exc

    message_id = sent.message_id
    await db.mark_published(post["id"], message_id, with_attribution)
    return message_id
