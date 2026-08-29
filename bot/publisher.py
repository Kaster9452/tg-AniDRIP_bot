"""Публикация предложенного поста в канал.

Контент копируется из личного чата с автором, а не из группы админов:
в группе к сообщению приклеена шапка с данными отправителя, и она не
должна попасть в канал.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Bot
from aiogram.types import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)

from bot import database as db
from bot.timeparse import format_when

logger = logging.getLogger(__name__)

# Типы контента, которым Telegram позволяет задать подпись
CAPTIONABLE_TYPES = {"photo", "video", "document", "audio", "animation", "voice"}

_INPUT_MEDIA_TYPES = {
    "photo": InputMediaPhoto,
    "video": InputMediaVideo,
    "document": InputMediaDocument,
    "audio": InputMediaAudio,
}

_SEND_METHODS = {
    "photo": "send_photo",
    "video": "send_video",
    "document": "send_document",
    "audio": "send_audio",
    "animation": "send_animation",
    "voice": "send_voice",
}


async def _send_by_file_id(
    bot: Bot, chat_id: int, content_type: str, file_id: str, caption: str | None
):
    method = getattr(bot, _SEND_METHODS[content_type])
    return await method(chat_id, file_id, caption=caption)


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


def channel_message_link(channel_id: int, message_id: int) -> str:
    """Ссылка на сообщение в канале вида t.me/c/... — работает без имени канала,
    даже если он приватный.
    """
    raw = str(channel_id)
    raw = raw[4:] if raw.startswith("-100") else raw.lstrip("-")
    return f"https://t.me/c/{raw}/{message_id}"


async def _notify_author(bot: Bot, post: asyncpg.Record, link: str) -> None:
    """Автор мог никогда больше не заглянуть в канал и не узнать о публикации сам —
    неудача доставки (блок бота и т.п.) не должна мешать самой публикации."""
    try:
        await bot.send_message(
            post["user_id"], f"✅ Твой пост опубликован в канале!\n{link}"
        )
    except Exception:
        logger.debug("Не удалось уведомить автора %s о публикации", post["user_id"])


async def notify_scheduled(
    bot: Bot, post: asyncpg.Record, when: datetime, tz: ZoneInfo
) -> None:
    """Автору в ЛС: его пост отложен и выйдет в канале в такое-то время.
    Свои посты админа не уведомляются, сбой доставки не мешает основному flow."""
    if post["is_own"]:
        return
    try:
        await bot.send_message(
            post["user_id"],
            f"🕓 Твой пост #{post['id']} отложен — выйдет в канале "
            f"{format_when(when, tz)}.",
        )
    except Exception:
        logger.debug(
            "Не удалось уведомить автора %s об откладке поста #%s",
            post["user_id"],
            post["id"],
        )


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
            # file_id уже есть на самом посте (в т.ч. у своих постов и после
            # редактирования медиа), а исходное сообщение автора могло исчезнуть.
            if post["file_id"]:
                sent = await _send_by_file_id(
                    bot, channel_id, content_type, post["file_id"], caption
                )
            else:
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

    # Свои посты админ уже видит в канале — уведомлять только внешних авторов.
    if not post["is_own"]:
        link = channel_message_link(channel_id, message_id)
        await _notify_author(bot, post, link)

    return message_id
