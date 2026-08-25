from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Важно: aiogram использует символ ":" как разделитель внутри callback_data,
# поэтому в значениях полей двоеточий быть не должно — каждое действие
# кодируется отдельным коротким словом.
ACT_PUBLISH = "pub"        # показать выбор подписи для публикации сейчас
ACT_LATER = "lat"          # показать выбор подписи для отложенной публикации
ACT_PUBLISH_ANON = "puba"
ACT_PUBLISH_SIGNED = "pubs"
ACT_LATER_ANON = "lata"
ACT_LATER_SIGNED = "lats"
ACT_BACK = "back"
ACT_CANCEL = "del"

# Действия, после которых пост публикуется немедленно
IMMEDIATE_ACTIONS = {ACT_PUBLISH_ANON, ACT_PUBLISH_SIGNED}
# Действия, после которых бот спрашивает время
SCHEDULE_ACTIONS = {ACT_LATER_ANON, ACT_LATER_SIGNED}
# Действия, означающие публикацию с подписью автора
SIGNED_ACTIONS = {ACT_PUBLISH_SIGNED, ACT_LATER_SIGNED}


class PostCB(CallbackData, prefix="p"):
    """Данные, зашитые в кнопки под предложкой."""

    action: str
    post_id: int


def main_keyboard(post_id: int) -> InlineKeyboardMarkup:
    """Две основные кнопки под каждым предложенным постом."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="📢 Опубликовать",
        callback_data=PostCB(action=ACT_PUBLISH, post_id=post_id),
    )
    builder.button(
        text="🕓 Отложить",
        callback_data=PostCB(action=ACT_LATER, post_id=post_id),
    )
    builder.adjust(2)
    return builder.as_markup()


def mode_keyboard(post_id: int, later: bool) -> InlineKeyboardMarkup:
    """Второй шаг: показывать автора или публиковать анонимно."""
    anon = ACT_LATER_ANON if later else ACT_PUBLISH_ANON
    signed = ACT_LATER_SIGNED if later else ACT_PUBLISH_SIGNED

    builder = InlineKeyboardBuilder()
    builder.button(text="🙈 Анонимно", callback_data=PostCB(action=anon, post_id=post_id))
    builder.button(text="✍️ С подписью", callback_data=PostCB(action=signed, post_id=post_id))
    builder.button(text="← Назад", callback_data=PostCB(action=ACT_BACK, post_id=post_id))
    builder.adjust(2, 1)
    return builder.as_markup()


def scheduled_keyboard(post_id: int) -> InlineKeyboardMarkup:
    """Под отложенным постом — возможность отменить публикацию."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Отменить публикацию",
                    callback_data=PostCB(action=ACT_CANCEL, post_id=post_id).pack(),
                )
            ]
        ]
    )


class SlotCB(CallbackData, prefix="s"):
    """Кнопки выбора времени для своего поста.

    day  — переключить день, value это индекс дня (0/1/2)
    slot — поставить пост, value это час
    stop — отменить черновик
    """

    action: str
    value: int


class ScheduleSlotCB(CallbackData, prefix="t"):
    """Кнопки выбора времени для предложенного поста."""

    action: str
    post_id: int
    value: int


def schedule_slots_keyboard(
    days: list[dict], day_index: int, post_id: int
) -> InlineKeyboardMarkup:
    """Сетка времени под вопросом о публикации предложенного поста."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=("· " + day["name"] + " ·") if day["index"] == day_index else day["name"],
                callback_data=ScheduleSlotCB(
                    action="day", post_id=post_id, value=day["index"]
                ).pack(),
            )
            for day in days
        ]
    ]

    current = days[day_index]
    row: list[InlineKeyboardButton] = []
    for slot in current["slots"]:
        if slot["state"] == "past":
            continue
        mark = ""
        if slot["state"] == "taken":
            # 🔴 — слот занял предложкой (не своим постом админа)
            mark = " 🔴" if slot.get("postOwn") is False else " 🟡"
        row.append(
            InlineKeyboardButton(
                text=f"{slot['time']}{mark}",
                callback_data=ScheduleSlotCB(
                    action="slot", post_id=post_id, value=slot["hour"]
                ).pack(),
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton(
                text="✖️ Отмена",
                callback_data=ScheduleSlotCB(
                    action="stop", post_id=post_id, value=0
                ).pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def own_slots_keyboard(days: list[dict], day_index: int) -> InlineKeyboardMarkup:
    """День сверху, свободные слоты ниже по три в ряд.

    Прошедшие слоты не показываем вовсе: в чате их некуда приглушить,
    а нажимать их всё равно нельзя.
    """
    rows: list[list[InlineKeyboardButton]] = []

    rows.append(
        [
            InlineKeyboardButton(
                text=("· " + day["name"] + " ·") if day["index"] == day_index else day["name"],
                callback_data=SlotCB(action="day", value=day["index"]).pack(),
            )
            for day in days
        ]
    )

    current = days[day_index]
    free = [slot for slot in current["slots"] if slot["state"] != "past"]

    row: list[InlineKeyboardButton] = []
    for slot in free:
        mark = ""
        if slot["state"] == "taken":
            # 🔴 — слот занял предложкой (не своим постом админа)
            mark = " 🔴" if slot.get("postOwn") is False else " 🟡"
        row.append(
            InlineKeyboardButton(
                text=f"{slot['time']}{mark}",
                callback_data=SlotCB(action="slot", value=slot["hour"]).pack(),
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton(
                text="✖️ Отменить", callback_data=SlotCB(action="stop", value=0).pack()
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
