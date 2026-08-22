import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    bot_token: str
    admin_group_id: int
    port: int


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN")
    group_id = os.getenv("ADMIN_GROUP_ID")
    # Render сам подставляет PORT для Web Service; для локального запуска
    # берём любое свободное значение по умолчанию.
    port = int(os.getenv("PORT", "10000"))

    if not token:
        raise RuntimeError(
            "BOT_TOKEN не задан. Скопируйте .env.example в .env и заполните его."
        )
    if not group_id:
        raise RuntimeError(
            "ADMIN_GROUP_ID не задан. Скопируйте .env.example в .env и заполните его."
        )

    try:
        group_id_int = int(group_id)
    except ValueError as exc:
        raise RuntimeError("ADMIN_GROUP_ID должен быть числом (например, -1001234567890)") from exc

    return Config(bot_token=token, admin_group_id=group_id_int, port=port)
