"""Свой пост через чат: то же самое, что экран «Свой пост» в панели.

Админ пишет текст, бот показывает сетку слотов кнопками, нажатие ставит
пост в очередь. Дальше его публикует обычный планировщик.
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot import database as db
from bot.handlers.callbacks import admin_label, is_group_admin
from bot.handlers.user import _extract_media
from bot.keyboards import SlotCB, own_slots_keyboard
from bot.slots import DAY_NAMES, OWN_TEXT_LIMIT, build_days, resolve_slot
from bot.timeparse import format_when

logger = logging.getLogger(__name__)

router = Router(name="ownpost")

CANCEL_WORDS = {"отмена", "cancel", "стоп"}
PREVIEW_LIMIT = 200
# Telegram ограничивает подпись к медиа 1024 символами — меньше, чем обычный текст.
CAPTION_TEXT_LIMIT = 1024
MEDIA_LABELS = {"photo": "Фото", "video": "Видео"}
OWN_FILES_LIMIT = 10

# Альбом приходит несколькими отдельными сообщениями с одинаковым
# media_group_id — собираем их немного, как и в обычной предложке.
ALBUM_WAIT_SECONDS = 1.2
_album_buffers: dict[str, list[Message]] = {}


class OwnPost(StatesGroup):
    waiting_for_text = State()
    waiting_for_slot = State()


def preview_of(plain_text: str) -> str:
    """Короткая цитата поста в сообщении с кнопками.

    Берём именно голый текст, а не размеченный: обрезка размеченного
    рано или поздно разрежет тег пополам, и Telegram откажется
    отправлять всё сообщение целиком. Форматирование в самом посте при
    этом сохраняется — оно живёт отдельно.
    """
    text = plain_text.strip()
    if len(text) > PREVIEW_LIMIT:
        text = text[:PREVIEW_LIMIT].rsplit(" ", 1)[0].rstrip() + "…"
    return html.escape(text)


async def show_slots(
    message: Message, state: FSMContext, tz: ZoneInfo, day_index: int = 0
) -> None:
    """Отправляет сетку кнопками. Текст поста уже лежит в состоянии."""
    data = await state.get_data()
    days = build_days(tz, datetime.now(tz), await db.get_scheduled_posts())

    await state.set_state(OwnPost.waiting_for_slot)
    await state.update_data(day=day_index)

    plain = data.get("plain", "")
    content_type = data.get("content_type", "text")
    media_group = data.get("media_group")
    if content_type == "text":
        summary = f"<blockquote>{preview_of(plain)}</blockquote>"
    else:
        label = f"Альбом · {len(media_group)}" if media_group else MEDIA_LABELS.get(content_type, content_type)
        summary = f"🖼 {label}" + (
            f" · <blockquote>{preview_of(plain)}</blockquote>" if plain else " без подписи"
        )

    await message.reply(
        f"🕓 <b>Когда публикуем?</b>\n\n"
        f"{summary}\n"
        f"🟡 — в слоте уже стоит свой пост, 🔴 — предложка. Ваш пост уйдёт следом.",
        reply_markup=own_slots_keyboard(days, day_index),
    )


@router.message(Command("mypost"))
async def cmd_mypost(
    message: Message,
    command: CommandObject,
    bot: Bot,
    state: FSMContext,
    tz: ZoneInfo,
    admin_group_id: int,
) -> None:
    if not await is_group_admin(bot, admin_group_id, message.from_user.id):
        await message.reply("Своими постами управляют только администраторы группы.")
        return

    # Текст можно дать сразу: /mypost Привет, канал
    if command.args and command.args.strip():
        # html_text содержит всю команду, отрезаем саму /mypost
        raw = message.html_text
        text = raw.split(maxsplit=1)[1].strip() if len(raw.split(maxsplit=1)) > 1 else ""
        await state.update_data(
            text=text, plain=command.args.strip(), content_type="text", file_id=None,
            media_group=None, has_draft=True
        )
        await show_slots(message, state, tz)
        return

    await state.set_state(OwnPost.waiting_for_text)
    await message.reply(
        "✍️ <b>Свой пост</b>\n\n"
        "Пришлите текст, фото или видео (можно альбомом) следующим сообщением — "
        "подпись и форматирование сохранятся.\n"
        "Передумали: напишите <code>отмена</code>."
    )


@router.message(OwnPost.waiting_for_text)
async def on_draft(message: Message, state: FSMContext, tz: ZoneInfo) -> None:
    if message.media_group_id:
        await _buffer_album_message(message, state, tz)
        return

    content_type = message.content_type
    plain = (message.text or message.caption or "").strip()

    if content_type == "text" and plain.lower() in CANCEL_WORDS:
        await state.clear()
        await message.reply("Черновик отменён.")
        return

    if content_type == "text":
        if not plain:
            await message.reply(
                "Нужен текст, фото или видео. Другие типы файлов через чат пока не умею — "
                "пришлите их предложкой или используйте /panel."
            )
            return
        if len(plain) > OWN_TEXT_LIMIT:
            await message.reply(
                f"Слишком длинный текст: {len(plain)} символов, а Telegram "
                f"пропускает {OWN_TEXT_LIMIT}."
            )
            return
        await state.update_data(
            text=message.html_text, plain=plain, content_type="text", file_id=None,
            media_group=None, has_draft=True
        )
        await show_slots(message, state, tz)
        return

    if content_type not in ("photo", "video"):
        await message.reply(
            "Пришлите текст, фото или видео. Другие типы файлов через чат пока не умею — "
            "пришлите их предложкой или используйте /panel."
        )
        return

    if len(plain) > CAPTION_TEXT_LIMIT:
        await message.reply(
            f"Слишком длинная подпись: {len(plain)} символов, а Telegram "
            f"пропускает {CAPTION_TEXT_LIMIT}."
        )
        return

    file_id, media_thumb_id = _extract_media(message, content_type)
    if not file_id:
        await message.reply("Не получилось прочитать файл, попробуйте ещё раз.")
        return

    await state.update_data(
        text=message.html_text if plain else "",
        plain=plain,
        content_type=content_type,
        file_id=file_id,
        media_thumb_id=media_thumb_id,
        media_group=None,
        has_draft=True,
    )
    await show_slots(message, state, tz)


async def _buffer_album_message(message: Message, state: FSMContext, tz: ZoneInfo) -> None:
    group_id = message.media_group_id
    is_first = group_id not in _album_buffers
    _album_buffers.setdefault(group_id, []).append(message)
    if is_first:
        asyncio.create_task(_flush_album(group_id, state, tz))


async def _flush_album(group_id: str, state: FSMContext, tz: ZoneInfo) -> None:
    await asyncio.sleep(ALBUM_WAIT_SECONDS)
    messages = _album_buffers.pop(group_id, [])
    if not messages:
        return
    messages.sort(key=lambda m: m.message_id)
    await _process_album(messages, state, tz)


async def _process_album(messages: list[Message], state: FSMContext, tz: ZoneInfo) -> None:
    first = messages[0]

    items: list[dict] = []
    for message in messages:
        if message.content_type not in ("photo", "video"):
            continue
        file_id, thumb_id = _extract_media(message, message.content_type)
        if file_id:
            items.append({"type": message.content_type, "file_id": file_id, "thumb": thumb_id})
    items = items[:OWN_FILES_LIMIT]

    if not items:
        await first.reply("Не получилось прочитать файлы альбома, попробуйте ещё раз.")
        return

    # Подпись к альбому Telegram кладёт только в одно из сообщений.
    caption_source = next((m for m in messages if m.caption), None)
    plain = (caption_source.caption if caption_source else "").strip()
    if len(plain) > CAPTION_TEXT_LIMIT:
        await first.reply(
            f"Слишком длинная подпись: {len(plain)} символов, а Telegram "
            f"пропускает {CAPTION_TEXT_LIMIT}."
        )
        return

    await state.update_data(
        text=caption_source.html_text if caption_source else "",
        plain=plain,
        content_type=items[0]["type"],
        file_id=items[0]["file_id"],
        media_thumb_id=items[0].get("thumb"),
        media_group=items if len(items) > 1 else None,
        has_draft=True,
    )
    await show_slots(first, state, tz)


@router.callback_query(SlotCB.filter(F.action == "stop"))
async def on_stop(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.message.edit_text("Черновик отменён.")
    await query.answer()


@router.callback_query(SlotCB.filter(F.action == "day"))
async def on_day(
    query: CallbackQuery, callback_data: SlotCB, state: FSMContext, tz: ZoneInfo
) -> None:
    data = await state.get_data()
    if not data.get("has_draft"):
        await query.answer("Черновик потерялся, начните заново: /mypost", show_alert=True)
        return

    await state.update_data(day=callback_data.value)
    days = build_days(tz, datetime.now(tz), await db.get_scheduled_posts())
    await query.message.edit_reply_markup(
        reply_markup=own_slots_keyboard(days, callback_data.value)
    )
    await query.answer(DAY_NAMES[callback_data.value])


@router.callback_query(SlotCB.filter(F.action == "slot"))
async def on_slot(
    query: CallbackQuery,
    callback_data: SlotCB,
    bot: Bot,
    state: FSMContext,
    tz: ZoneInfo,
    admin_group_id: int,
) -> None:
    if not await is_group_admin(bot, admin_group_id, query.from_user.id):
        await query.answer("Только администраторы группы.", show_alert=True)
        return

    data = await state.get_data()
    if not data.get("has_draft"):
        await query.answer("Черновик потерялся, начните заново: /mypost", show_alert=True)
        return
    text = data.get("text") or ""

    now_local = datetime.now(tz)
    when = resolve_slot(tz, now_local, data.get("day", 0), callback_data.value)
    if when <= now_local:
        await query.answer("Это время уже прошло, выберите другое.", show_alert=True)
        return

    post_id = await db.create_own_post(
        user_id=query.from_user.id,
        author_name=query.from_user.full_name,
        author_username=query.from_user.username,
        content_html=text,
        scheduled_at=when.astimezone(timezone.utc),
        content_type=data.get("content_type", "text"),
        file_id=data.get("file_id"),
        media_thumb_id=data.get("media_thumb_id"),
        media_group=data.get("media_group"),
        scheduled_by=admin_label(query.from_user),
    )
    await state.clear()

    await query.message.edit_text(
        f"🕓 <b>Свой пост #{post_id}</b> встанет в канал "
        f"<b>{format_when(when, tz)}</b>.\n\n"
        f"<blockquote>{preview_of(data.get('plain', ''))}</blockquote>\n"
        f"Передумали — <code>/cancel {post_id}</code>."
    )
    await query.answer("Готово")
