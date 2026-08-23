"""Разбор даты и времени, которые админ вводит текстом.

Поддерживаемые форматы:
    18:30                 — сегодня в 18:30 (если время уже прошло — завтра)
    завтра 09:00          — завтра в 09:00
    23.08 18:30           — 23 августа текущего года
    23.08.2026 18:30      — конкретная дата
    +30м / +2ч / +1д      — через указанный интервал от текущего момента
    +1ч30м                — комбинированный интервал
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


class TimeParseError(ValueError):
    """Не удалось разобрать введённое время."""


# Длинные варианты идут первыми, иначе «мин» совпадёт как «м» + остаток.
# Границу слова \b здесь использовать нельзя: между буквой и цифрой
# в «+1ч30м» её нет, и разбор комбинированных интервалов ломается.
_RELATIVE_RE = re.compile(
    r"^\+\s*"
    r"(?:(?P<days>\d+)\s*(?:дней|дня|дн|д|days|day|d))?\s*"
    r"(?:(?P<hours>\d+)\s*(?:часов|часа|час|ч|hours|hour|h))?\s*"
    r"(?:(?P<minutes>\d+)\s*(?:минуты|минут|мин|м|minutes|min|m))?\s*$",
    re.IGNORECASE,
)

_TIME_RE = re.compile(r"^(?P<hour>\d{1,2})[:.](?P<minute>\d{2})$")

_DATE_TIME_RE = re.compile(
    r"^(?P<day>\d{1,2})[.\-/](?P<month>\d{1,2})"
    r"(?:[.\-/](?P<year>\d{2,4}))?"
    r"[\s,]+(?P<hour>\d{1,2})[:.](?P<minute>\d{2})$"
)


def _build(
    tz: ZoneInfo, year: int, month: int, day: int, hour: int, minute: int
) -> datetime:
    try:
        return datetime(year, month, day, hour, minute, tzinfo=tz)
    except ValueError as exc:
        raise TimeParseError(f"Такой даты не существует: {day:02d}.{month:02d}.{year}") from exc


def parse_when(raw: str, tz: ZoneInfo, now: datetime | None = None) -> datetime:
    """Возвращает момент публикации с учётом часового пояса.

    Бросает TimeParseError, если строку разобрать не удалось или момент
    оказался в прошлом.
    """
    text = raw.strip().lower().replace("ё", "е")
    if not text:
        raise TimeParseError("Пустая строка")

    current = now.astimezone(tz) if now else datetime.now(tz)

    # относительный интервал: +2ч, +30м, +1д, +1ч30м
    if text.startswith("+"):
        match = _RELATIVE_RE.match(text)
        if not match or not any(match.groupdict().values()):
            raise TimeParseError(
                "Не понял интервал. Примеры: +30м, +2ч, +1д, +1ч30м"
            )
        delta = timedelta(
            days=int(match.group("days") or 0),
            hours=int(match.group("hours") or 0),
            minutes=int(match.group("minutes") or 0),
        )
        if delta <= timedelta(0):
            raise TimeParseError("Интервал должен быть больше нуля")
        return current + delta

    # словесный сдвиг дня: "завтра 09:00", "сегодня 18:30", "послезавтра 12:00"
    day_offset = 0
    for word, offset in (("послезавтра", 2), ("завтра", 1), ("сегодня", 0)):
        if text.startswith(word):
            day_offset = offset
            text = text[len(word) :].strip()
            break
    else:
        word = None

    # только время: 18:30
    match = _TIME_RE.match(text)
    if match:
        hour, minute = int(match.group("hour")), int(match.group("minute"))
        if hour > 23 or minute > 59:
            raise TimeParseError("Время должно быть в диапазоне 00:00–23:59")
        base = current + timedelta(days=day_offset)
        result = _build(tz, base.year, base.month, base.day, hour, minute)
        # без явного слова "сегодня/завтра" прошедшее время значит завтра
        if result <= current and word is None:
            result += timedelta(days=1)
        return result

    if word is not None:
        raise TimeParseError("После слова укажите время, например: завтра 09:00")

    # дата и время: 23.08 18:30 или 23.08.2026 18:30
    match = _DATE_TIME_RE.match(text)
    if match:
        day, month = int(match.group("day")), int(match.group("month"))
        hour, minute = int(match.group("hour")), int(match.group("minute"))
        if hour > 23 or minute > 59:
            raise TimeParseError("Время должно быть в диапазоне 00:00–23:59")

        year_raw = match.group("year")
        if year_raw is None:
            year = current.year
        else:
            year = int(year_raw)
            if year < 100:
                year += 2000

        result = _build(tz, year, month, day, hour, minute)
        # дата без года, которая уже прошла, — значит следующий год
        if year_raw is None and result <= current:
            result = _build(tz, year + 1, month, day, hour, minute)
        return result

    raise TimeParseError(
        "Не понял формат. Примеры: 18:30, завтра 09:00, 23.08 18:30, +2ч"
    )


def format_when(moment: datetime, tz: ZoneInfo) -> str:
    """Человекочитаемое время для сообщений админам."""
    return moment.astimezone(tz).strftime("%d.%m.%Y %H:%M")
