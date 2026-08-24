"""Свой пост через чат: то же самое, что экран «Свой пост» в панели.

Админ пишет текст, бот показывает сетку слотов кнопками, нажатие ставит
пост в очередь. Дальше его публикует обычный планировщик.
"""

from __future__ import annotations

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
from bot.keyboards import SlotCB, own_slots_keyboard
from bot.slots import DAY_NAMES, OWN_TEXT_LIMIT, build_days, resolve_slot
from bot.timeparse import format_when

logger = logging.getLogger(__name__)

router = Router(name="ownpost")

CANCEL_WORDS = {"отмена", "cancel", "стоп"}
PREVIEW_LIMIT = 200


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

    await message.reply(
        f"🕓 <b>Когда публикуем?</b>\n\n"
        f"<blockquote>{preview_of(data.get('plain', ''))}</blockquote>\n"
        f"🟡 — в слоте уже стоит пост, ваш уйдёт следом.",
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
        await state.update_data(text=text, plain=command.args.strip())
        await show_slots(message, state, tz)
        return

    await state.set_state(OwnPost.waiting_for_text)
    await message.reply(
        "✍️ <b>Свой пост</b>\n\n"
        "Пришлите текст следующим сообщением — форматирование сохранится.\n"
        "Передумали: напишите <code>отмена</code>."
    )


@router.message(OwnPost.waiting_for_text)
async def on_text(message: Message, state: FSMContext, tz: ZoneInfo) -> None:
    plain = (message.text or "").strip()

    if plain.lower() in CANCEL_WORDS:
        await state.clear()
        await message.reply("Черновик отменён.")
        return

    if not plain:
        await message.reply(
            "Нужен обычный текст. Фото и файлы через чат пока не умею — "
            "пришлите их предложкой или используйте /panel."
        )
        return

    if len(plain) > OWN_TEXT_LIMIT:
        await message.reply(
            f"Слишком длинный текст: {len(plain)} символов, а Telegram "
            f"пропускает {OWN_TEXT_LIMIT}."
        )
        return

    await state.update_data(text=message.html_text, plain=plain)
    await show_slots(message, state, tz)


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
    if "text" not in data:
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
    text = data.get("text")
    if not text:
        await query.answer("Черновик потерялся, начните заново: /mypost", show_alert=True)
        return

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
