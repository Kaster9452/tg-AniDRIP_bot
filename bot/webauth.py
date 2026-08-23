"""Проверка подлинности данных, которые Mini App присылает на сервер.

Telegram подписывает initData ключом, производным от токена бота. Без этой
проверки любой человек мог бы отправить нашему серверу запрос «опубликуй
пост от имени админа» — поэтому проверка обязательна для каждого запроса,
а не только при открытии панели.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_AGE_SECONDS = 24 * 60 * 60


class InitDataError(ValueError):
    """Данные не прошли проверку подлинности."""


def _check_string(fields: dict[str, str], skip: set[str]) -> str:
    return "\n".join(
        f"{key}={value}" for key, value in sorted(fields.items()) if key not in skip
    )


def validate_init_data(
    init_data: str, bot_token: str, max_age_seconds: int = MAX_AGE_SECONDS
) -> dict:
    """Проверяет подпись и возвращает данные пользователя.

    Бросает InitDataError, если подпись неверна, данные устарели или
    в них нет пользователя.
    """
    if not init_data:
        raise InitDataError("Пустые данные авторизации")

    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = fields.get("hash")
    if not received_hash:
        raise InitDataError("В данных нет подписи")

    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()

    # Обычно в подпись входят все поля, кроме hash. В новых версиях Telegram
    # добавилось поле signature для сторонней проверки — на части клиентов
    # оно в HMAC не участвует, поэтому проверяем оба варианта.
    candidates = [{"hash"}, {"hash", "signature"}]
    for skip in candidates:
        expected = hmac.new(
            secret, _check_string(fields, skip).encode(), hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(expected, received_hash):
            break
    else:
        raise InitDataError("Подпись не совпадает")

    raw_auth_date = fields.get("auth_date")
    if not raw_auth_date:
        raise InitDataError("В данных нет времени авторизации")
    try:
        auth_date = int(raw_auth_date)
    except ValueError as exc:
        raise InitDataError("Некорректное время авторизации") from exc

    if max_age_seconds and time.time() - auth_date > max_age_seconds:
        raise InitDataError("Сессия устарела, откройте панель заново")

    raw_user = fields.get("user")
    if not raw_user:
        raise InitDataError("В данных нет пользователя")
    try:
        user = json.loads(raw_user)
    except json.JSONDecodeError as exc:
        raise InitDataError("Не удалось разобрать данные пользователя") from exc

    if not isinstance(user, dict) or "id" not in user:
        raise InitDataError("В данных нет идентификатора пользователя")

    return user
