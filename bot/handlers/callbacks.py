import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot import database as db
from bot.keyboards import (
    ACT_BACK,
    ACT_CANCEL,
    ACT_LATER,
    ACT_PUBLISH,
    IMMEDIATE_ACTIONS,
    SCHEDULE_ACTIONS,
    SIGNED_ACTIONS,
    PostCB,
    ScheduleSlotCB,
    main_keyboard,
    mode_keyboard,
    schedule_slots_keyboard,
    scheduled_keyboard,
)
from bot.publisher import PublishError, publish_post
from bot.slots import build_days, resolve_slot
from bot.timeparse import TimeParseError, format_when, parse_when

logger = logging.getLogger(__name__)

router = Router(name="callbacks")

NOT_ADMIN = "Только администраторы группы могут это делать."


class ScheduleStates(StatesGroup):
    waiting_for_time = State()


async def is_group_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Кнопками и командами управления может пользоваться только админ группы."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        logger.exception("Не удалось проверить права пользователя %s", user_id)
        return False
    return member.status in ("creator", "administrator")

def admin_label(user) -> str:
    """Имя админа для отображения в очереди — кто отложил пост."""
    return f"@{user.username}" if user.username else user.full_name

async def _guard(query: CallbackQuery, bot: Bot, post_id: int, admin_group_id: int):
    """Общая проверка: права админа и состояние поста.
    Возвращает запись поста или None, если действие запрещено."""
    if not await is_group_admin(bot, admin_group_id, query.from_user.id):
        await query.answer(NOT_ADMIN, show_alert=True)
        return None

    post = await db.get_post(post_id)
    if post is None:
        await query.answer("Пост не найден в базе.", show_alert=True)
        return None
    if post["status"] == "published":
        await query.answer("Этот пост уже опубликован.", show_alert=True)
        return None
    return post


@router.callback_query(PostCB.filter(F.action.in_({ACT_PUBLISH, ACT_LATER})))
async def on_first_step(query: CallbackQuery, callback_data: PostCB, bot: Bot, admin_group_id: int) -> None:
    post = await _guard(query, bot, callback_data.post_id, admin_group_id)
    if post is None:
        return

    later = callback_data.action == ACT_LATER
    await query.message.edit_reply_markup(
        reply_markup=mode_keyboard(callback_data.post_id, later=later)
    )
    await query.answer("Как публикуем?")


@router.callback_query(PostCB.filter(F.action == ACT_BACK))
async def on_back(query: CallbackQuery, callback_data: PostCB) -> None:
    await query.message.edit_reply_markup(
        reply_markup=main_keyboard(callback_data.post_id)
    )
    await query.answer()


@router.callback_query(PostCB.filter(F.action.in_(IMMEDIATE_ACTIONS)))
async def on_publish_now(
    query: CallbackQuery, callback_data: PostCB, bot: Bot, channel_id: int, admin_group_id: int
) -> None:
    post = await _guard(query, bot, callback_data.post_id, admin_group_id)
    if post is None:
        return

    with_attribution = callback_data.action in SIGNED_ACTIONS

    try:
        await publish_post(bot, post, channel_id, with_attribution)
    except PublishError as exc:
        await query.answer("Не удалось опубликовать.", show_alert=True)
        await query.message.reply(f"⚠️ Ошибка публикации поста #{post['id']}: {exc}")
        return

    await query.message.edit_reply_markup(reply_markup=None)
    signature = "с подписью" if with_attribution else "анонимно"
    who = query.from_user.username or query.from_user.full_name
    await query.message.reply(f"✅ Пост #{post['id']} опубликован ({signature}) — {who}")
    await query.answer("Опубликовано")


@router.callback_query(PostCB.filter(F.action.in_(SCHEDULE_ACTIONS)))
async def on_schedule_requested(
    query: CallbackQuery,
    callback_data: PostCB,
    bot: Bot,
    state: FSMContext,
    tz: ZoneInfo,
    admin_group_id: int,
) -> None:
    post = await _guard(query, bot, callback_data.post_id, admin_group_id)
    if post is None:
        return

    with_attribution = callback_data.action in SIGNED_ACTIONS

    await state.set_state(ScheduleStates.waiting_for_time)
    await state.update_data(post_id=post["id"], with_attribution=with_attribution)
    days = build_days(tz, datetime.now(tz), await db.get_scheduled_posts())
    await query.message.edit_reply_markup(
        reply_markup=schedule_slots_keyboard(days, 0, post["id"])
    )
    await query.answer("Выберите день и время публикации")


@router.callback_query(ScheduleSlotCB.filter(F.action == "day"))
async def on_schedule_day(
    query: CallbackQuery,
    callback_data: ScheduleSlotCB,
    bot: Bot,
    state: FSMContext,
    tz: ZoneInfo,
    admin_group_id: int,
) -> None:
    if not await is_group_admin(bot, admin_group_id, query.from_user.id):
        await query.answer(NOT_ADMIN, show_alert=True)
        return

    data = await state.get_data()
    if data.get("post_id") != callback_data.post_id:
        await query.answer("Выбор времени уже неактуален.", show_alert=True)
        return

    days = build_days(tz, datetime.now(tz), await db.get_scheduled_posts())
    if callback_data.value not in range(len(days)):
        await query.answer("Такого дня нет.", show_alert=True)
        return

    await state.update_data(schedule_day=callback_data.value)
    await query.message.edit_reply_markup(
        reply_markup=schedule_slots_keyboard(
            days, callback_data.value, callback_data.post_id
        )
    )
    await query.answer(days[callback_data.value]["name"])


@router.callback_query(ScheduleSlotCB.filter(F.action == "slot"))
async def on_schedule_slot(
    query: CallbackQuery,
    callback_data: ScheduleSlotCB,
    bot: Bot,
    state: FSMContext,
    tz: ZoneInfo,
    admin_group_id: int,
) -> None:
    if not await is_group_admin(bot, admin_group_id, query.from_user.id):
        await query.answer(NOT_ADMIN, show_alert=True)
        return

    data = await state.get_data()
    if data.get("post_id") != callback_data.post_id:
        await query.answer("Выбор времени уже неактуален.", show_alert=True)
        return

    now_local = datetime.now(tz)
    day_index = data.get("schedule_day", 0)
    when = resolve_slot(tz, now_local, day_index, callback_data.value)
    if when <= now_local:
        await query.answer("Это время уже прошло.", show_alert=True)
        return

    post = await db.get_post(callback_data.post_id)
    if post is None or post["status"] == "published":
        await state.clear()
        await query.answer("Пост уже опубликован или удалён.", show_alert=True)
        return

    with_attribution = data["with_attribution"]
    await db.mark_scheduled(
        callback_data.post_id,
        when.astimezone(timezone.utc),
        with_attribution,
        admin_label(query.from_user),
    )
    await state.clear()

    if post["admin_chat_id"] and post["admin_message_id"]:
        try:
            await bot.edit_message_reply_markup(
                chat_id=post["admin_chat_id"],
                message_id=post["admin_message_id"],
                reply_markup=scheduled_keyboard(callback_data.post_id),
            )
        except Exception:
            logger.debug("Не удалось обновить кнопки у поста %s", callback_data.post_id)

    signature = "с подписью" if with_attribution else "анонимно"
    await query.message.edit_reply_markup(
        reply_markup=scheduled_keyboard(callback_data.post_id)
    )
    await query.answer(
        f"Запланировано на {format_when(when, tz)} ({signature})", show_alert=True
    )


@router.callback_query(ScheduleSlotCB.filter(F.action == "stop"))
async def on_schedule_stop(
    query: CallbackQuery, callback_data: ScheduleSlotCB, state: FSMContext
) -> None:
    data = await state.get_data()
    if data.get("post_id") != callback_data.post_id:
        await query.answer("Выбор времени уже неактуален.", show_alert=True)
        return

    await state.clear()
    await query.message.edit_reply_markup(
        reply_markup=main_keyboard(callback_data.post_id)
    )
    await query.answer("Выбор времени отменён")


@router.message(ScheduleStates.waiting_for_time)
async def on_time_entered(
    message: Message, state: FSMContext, bot: Bot, tz: ZoneInfo
) -> None:
    text = (message.text or "").strip()

    if text.lower() in ("отмена", "cancel", "/cancel"):
        await state.clear()
        await message.reply("Отложенная публикация отменена.")
        return

    try:
        when = parse_when(text, tz)
    except TimeParseError as exc:
        await message.reply(
            f"⚠️ {exc}\n\nПопробуйте ещё раз или напишите <code>отмена</code>."
        )
        return

    if when <= datetime.now(tz):
        await message.reply("⚠️ Это время уже прошло. Укажите будущее время.")
        return

    data = await state.get_data()
    post_id = data["post_id"]
    with_attribution = data["with_attribution"]
    await state.clear()

    await db.mark_scheduled(
        post_id, when.astimezone(timezone.utc), with_attribution, admin_label(message.from_user)
    )

    post = await db.get_post(post_id)
    if post and post["admin_chat_id"] and post["admin_message_id"]:
        try:
            await bot.edit_message_reply_markup(
                chat_id=post["admin_chat_id"],
                message_id=post["admin_message_id"],
                reply_markup=scheduled_keyboard(post_id),
            )
        except Exception:
            logger.debug("Не удалось обновить кнопки у поста %s", post_id)

    signature = "с подписью" if with_attribution else "анонимно"
    await message.reply(
        f"🕓 Пост #{post_id} запланирован на <b>{format_when(when, tz)}</b> ({signature})."
    )


@router.callback_query(PostCB.filter(F.action == ACT_CANCEL))
async def on_cancel_scheduled(
    query: CallbackQuery, callback_data: PostCB, bot: Bot, admin_group_id: int
) -> None:
    if not await is_group_admin(bot, admin_group_id, query.from_user.id):
        await query.answer(NOT_ADMIN, show_alert=True)
        return

    post = await db.get_post(callback_data.post_id)
    if post is None:
        await query.answer("Пост не найден.", show_alert=True)
        return
    if post["status"] != "scheduled":
        await query.answer("Этот пост не запланирован.", show_alert=True)
        return

    await db.mark_cancelled(callback_data.post_id)
    await query.message.edit_reply_markup(
        reply_markup=main_keyboard(callback_data.post_id)
    )
    admin_label = f"@{query.from_user.username}" if query.from_user.username else query.from_user.first_name
    post_type_label = "Свой пост" if post.get("is_own") else "Пост"
    await query.message.reply(f"🗑 {post_type_label} #{callback_data.post_id} снят с отложки ({admin_label}).")
    await query.answer("Отменено")
