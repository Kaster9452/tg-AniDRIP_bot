"""Сетка эфира: двенадцать слотов в сутки, через каждые два часа.

Один и тот же расчёт нужен и панели, и кнопкам в чате, поэтому он живёт
отдельно — иначе две копии рано или поздно разойдутся.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Слоты по нечётным часам.
SLOT_HOURS = [1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23]
# Порядок показа: сверху поздние.
SLOT_ORDER = list(reversed(SLOT_HOURS))
DAY_NAMES = ["сегодня", "завтра", "послезавтра"]
OWN_TEXT_LIMIT = 4096


def slot_of(moment: datetime) -> tuple:
    """К какому слоту относится момент времени.

    Слоты нечётные и идут через два часа, значит пост в 20:40 попадает
    в слот 19:00, а всё, что до часу ночи, — в 23:00 прошлого дня.
    """
    hour = moment.hour if moment.hour % 2 == 1 else moment.hour - 1
    day = moment.date()
    if hour < 0:
        hour, day = 23, day - timedelta(days=1)
    return day, hour


def resolve_slot(tz: ZoneInfo, now_local: datetime, day_offset: int, hour: int) -> datetime:
    """Превращает «день + час» в конкретный момент публикации."""
    date = (now_local + timedelta(days=day_offset)).date()
    return datetime(date.year, date.month, date.day, hour, tzinfo=tz)


def build_days(tz: ZoneInfo, now_local: datetime, scheduled: list) -> list[dict]:
    """Три дня по двенадцать слотов: что занято, что прошло, что свободно."""
    taken: dict[tuple, int] = {}
    for post in scheduled:
        if not post["scheduled_at"]:
            continue
        key = slot_of(post["scheduled_at"].astimezone(tz))
        # если в слоте уже что-то есть, показываем самый ранний пост
        taken.setdefault(key, post["id"])

    days = []
    for offset, name in enumerate(DAY_NAMES):
        date = (now_local + timedelta(days=offset)).date()
        slots = []
        for hour in SLOT_ORDER:
            start = datetime(date.year, date.month, date.day, hour, tzinfo=tz)
            post_id = taken.get((date, hour))
            if start <= now_local:
                state = "past"
            elif post_id:
                state = "taken"
            else:
                state = "free"
            slots.append(
                {
                    "hour": hour,
                    "time": f"{hour:02d}:00",
                    "state": state,
                    "postId": post_id,
                }
            )
        days.append(
            {
                "index": offset,
                "name": name,
                "date": date.strftime("%d.%m"),
                "slots": slots,
            }
        )
    return days


def next_free_slot(days: list[dict]) -> dict | None:
    """Ближайшее свободное окно — его показывает кнопка «Свой пост»."""
    for day in days:
        # внутри дня слоты идут от поздних к ранним, а нам нужен ближайший
        for slot in reversed(day["slots"]):
            if slot["state"] == "free":
                return {
                    "time": slot["time"],
                    "day": day["index"],
                    "dayName": day["name"],
                }
    return None
