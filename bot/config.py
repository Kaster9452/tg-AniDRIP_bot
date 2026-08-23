import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    bot_token: str
    admin_group_id: int
    channel_id: int
    database_url: str
    timezone: ZoneInfo
    timezone_name: str
    webapp_url: str
    webapp_short_name: str
    port: int


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"{name} не задан. Добавьте эту переменную в настройках Render "
            f"(Environment) или в локальный файл .env"
        )
    return value


def _as_int(name: str, value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(
            f"{name} должен быть числом (например, -1001234567890), а получено: {value}"
        ) from exc


def load_config() -> Config:
    token = _required("BOT_TOKEN")
    group_id = _as_int("ADMIN_GROUP_ID", _required("ADMIN_GROUP_ID"))
    channel_id = _as_int("CHANNEL_ID", _required("CHANNEL_ID"))
    database_url = _required("DATABASE_URL")

    tz_name = os.getenv("TIMEZONE", "Europe/Stockholm")
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:
        raise RuntimeError(
            f"Неизвестный часовой пояс TIMEZONE={tz_name}. "
            f"Пример правильного значения: Europe/Stockholm"
        ) from exc

    # Адрес самого сервиса на Render — нужен, чтобы открыть панель как
    # Mini App. Telegram требует именно https.
    webapp_url = os.getenv("WEBAPP_URL", "").rstrip("/")

    # Короткое имя Mini App из BotFather. Нужно только для запуска
    # панели из группы: обычные кнопки Mini App Telegram там не
    # разрешает, а прямая ссылка t.me/бот/имя работает везде.
    webapp_short_name = os.getenv("WEBAPP_SHORT_NAME", "").strip()

    # Render сам подставляет PORT для Web Service; для локального запуска
    # берём значение по умолчанию.
    port = int(os.getenv("PORT", "10000"))

    return Config(
        bot_token=token,
        admin_group_id=group_id,
        channel_id=channel_id,
        database_url=database_url,
        timezone=tz,
        timezone_name=tz_name,
        webapp_url=webapp_url,
        webapp_short_name=webapp_short_name,
        port=port,
    )
