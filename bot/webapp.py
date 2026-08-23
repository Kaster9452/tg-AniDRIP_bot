"""HTTP-часть: раздача админ-панели и API для неё.

Каждый запрос от панели проходит две проверки:
1) подпись Telegram (initData) — что запрос действительно из Mini App;
2) права в группе админов — что этот пользователь вправе публиковать.

Вторую проверку нельзя опустить: подпись подтверждает лишь, кто перед нами,
но не то, что этому человеку что-то позволено.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiohttp import web

from bot import database as db
from bot.config import Config
from bot.publisher import PublishError, publish_post
from bot.timeparse import TimeParseError, format_when, parse_when
from bot.webauth import InitDataError, validate_init_data

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# --- вспомогательное форматирование для интерфейса --------------------------


def relative_ago(moment: datetime, now: datetime) -> str:
    delta = now - moment
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч назад"
    days = hours // 24
    if days == 1:
        return "вчера"
    return f"{days} дн назад"


def day_label(moment: datetime, now: datetime) -> str:
    today = now.date()
    target = moment.date()
    if target == today:
        return "сегодня"
    if target == today + timedelta(days=1):
        return "завтра"
    if target == today - timedelta(days=1):
        return "вчера"
    return moment.strftime("%d.%m")


def author_of(post) -> str:
    if post["author_username"]:
        return f"@{post['author_username']}"
    return post["author_name"] or "без имени"


def initials_of(post) -> str:
    source = post["author_username"] or post["author_name"] or "?"
    return source.lstrip("@")[:2].lower()


def preview_of(post, limit: int = 160) -> str:
    text = (post["content_html"] or "").strip()
    if not text:
        return ""
    if len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


MEDIA_LABELS = {
    "photo": "фото",
    "video": "видео",
    "document": "файл",
    "audio": "аудио",
    "animation": "гиф",
    "voice": "голосовое",
    "sticker": "стикер",
    "video_note": "кружок",
}


def serialize(post, tz: ZoneInfo, now_local: datetime) -> dict:
    created = post["created_at"].astimezone(tz)
    data = {
        "id": post["id"],
        "author": author_of(post),
        "initials": initials_of(post),
        "preview": preview_of(post),
        "media": MEDIA_LABELS.get(post["content_type"]),
        "ago": relative_ago(created, now_local),
        "signed": bool(post["with_attribution"]),
    }
    if post["scheduled_at"]:
        moment = post["scheduled_at"].astimezone(tz)
        data["time"] = moment.strftime("%H:%M")
        data["day"] = day_label(moment, now_local)
        data["when_full"] = format_when(moment, tz)
    if post["published_at"]:
        moment = post["published_at"].astimezone(tz)
        data["time"] = moment.strftime("%H:%M")
        data["day"] = day_label(moment, now_local)
    return data


# --- авторизация ------------------------------------------------------------


async def authorize(request: web.Request, payload: dict) -> dict:
    config: Config = request.app["config"]
    bot: Bot = request.app["bot"]

    try:
        user = validate_init_data(payload.get("initData", ""), config.bot_token)
    except InitDataError as exc:
        raise ApiError(str(exc), status=401) from exc

    try:
        member = await bot.get_chat_member(config.admin_group_id, user["id"])
    except Exception as exc:
        logger.exception("Не удалось проверить права пользователя %s", user["id"])
        raise ApiError("Не удалось проверить права в группе", status=403) from exc

    if member.status not in ("creator", "administrator"):
        raise ApiError("Панель доступна только администраторам группы", status=403)

    return user


async def read_payload(request: web.Request) -> dict:
    try:
        payload = await request.json()
    except Exception as exc:
        raise ApiError("Некорректный запрос") from exc
    if not isinstance(payload, dict):
        raise ApiError("Некорректный запрос")
    return payload


def post_id_from(payload: dict) -> int:
    try:
        return int(payload["postId"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError("Не указан номер поста") from exc


async def load_post(post_id: int):
    post = await db.get_post(post_id)
    if post is None:
        raise ApiError("Пост не найден", status=404)
    return post


# --- обработчики ------------------------------------------------------------


async def handle_ping(request: web.Request) -> web.Response:
    """Пустой ответ для внешнего будильника и проверки Render."""
    return web.Response(text="Bot is running")


async def handle_app(request: web.Request) -> web.Response:
    index = STATIC_DIR / "index.html"
    if not index.exists():
        return web.Response(status=404, text="Панель не найдена")
    return web.FileResponse(index)


async def handle_state(request: web.Request) -> web.Response:
    payload = await read_payload(request)
    await authorize(request, payload)

    config: Config = request.app["config"]
    tz = config.timezone
    now_local = datetime.now(tz)
    day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    pending = await db.get_pending_posts(limit=30)
    queue = await db.get_scheduled_posts()
    published = await db.get_published_posts(limit=15)
    stats = await db.count_stats(day_start.astimezone(timezone.utc))

    return web.json_response(
        {
            "stats": stats,
            "inbox": [serialize(p, tz, now_local) for p in pending],
            "queue": [serialize(p, tz, now_local) for p in queue],
            "log": [serialize(p, tz, now_local) for p in published],
            "timezone": config.timezone_name,
        }
    )


async def handle_publish(request: web.Request) -> web.Response:
    payload = await read_payload(request)
    await authorize(request, payload)

    config: Config = request.app["config"]
    bot: Bot = request.app["bot"]

    post = await load_post(post_id_from(payload))
    if post["status"] == "published":
        raise ApiError("Этот пост уже опубликован")

    signed = bool(payload.get("signed"))
    try:
        await publish_post(bot, post, config.channel_id, signed)
    except PublishError as exc:
        raise ApiError(f"Не удалось опубликовать: {exc}", status=502) from exc

    await clear_admin_buttons(bot, post)
    await notify_group(
        bot,
        config,
        f"✅ Пост #{post['id']} опубликован через панель "
        f"({'с подписью' if signed else 'анонимно'}).",
    )
    return web.json_response({"ok": True, "message": "Опубликовано"})


async def handle_schedule(request: web.Request) -> web.Response:
    payload = await read_payload(request)
    await authorize(request, payload)

    config: Config = request.app["config"]
    bot: Bot = request.app["bot"]
    tz = config.timezone

    post = await load_post(post_id_from(payload))
    if post["status"] == "published":
        raise ApiError("Этот пост уже опубликован")

    raw_when = str(payload.get("when", "")).strip()
    try:
        when = parse_when(raw_when, tz)
    except TimeParseError as exc:
        raise ApiError(str(exc)) from exc

    if when <= datetime.now(tz):
        raise ApiError("Это время уже прошло")

    signed = bool(payload.get("signed"))
    await db.mark_scheduled(post["id"], when.astimezone(timezone.utc), signed)
    await set_scheduled_buttons(bot, post)

    return web.json_response(
        {"ok": True, "message": f"Запланировано на {format_when(when, tz)}"}
    )


async def handle_cancel(request: web.Request) -> web.Response:
    payload = await read_payload(request)
    await authorize(request, payload)

    bot: Bot = request.app["bot"]
    post = await load_post(post_id_from(payload))
    if post["status"] != "scheduled":
        raise ApiError("Этот пост не запланирован")

    await db.mark_cancelled(post["id"])
    await restore_admin_buttons(bot, post)
    return web.json_response({"ok": True, "message": "Снято с публикации"})


async def handle_reject(request: web.Request) -> web.Response:
    payload = await read_payload(request)
    await authorize(request, payload)

    bot: Bot = request.app["bot"]
    post = await load_post(post_id_from(payload))
    if post["status"] == "published":
        raise ApiError("Этот пост уже опубликован")

    await db.mark_rejected(post["id"])
    await clear_admin_buttons(bot, post)
    return web.json_response({"ok": True, "message": "Отклонено"})


# --- синхронизация с кнопками в группе --------------------------------------


async def clear_admin_buttons(bot: Bot, post) -> None:
    await _set_buttons(bot, post, None)


async def restore_admin_buttons(bot: Bot, post) -> None:
    from bot.keyboards import main_keyboard

    await _set_buttons(bot, post, main_keyboard(post["id"]))


async def set_scheduled_buttons(bot: Bot, post) -> None:
    from bot.keyboards import scheduled_keyboard

    await _set_buttons(bot, post, scheduled_keyboard(post["id"]))


async def _set_buttons(bot: Bot, post, markup) -> None:
    """Панель и группа показывают одни и те же посты, поэтому действие
    в панели должно менять и кнопки под сообщением в группе."""
    if not post["admin_chat_id"] or not post["admin_message_id"]:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=post["admin_chat_id"],
            message_id=post["admin_message_id"],
            reply_markup=markup,
        )
    except Exception:
        logger.debug("Не удалось обновить кнопки у поста %s", post["id"])


async def notify_group(bot: Bot, config: Config, text: str) -> None:
    try:
        await bot.send_message(config.admin_group_id, text)
    except Exception:
        logger.debug("Не удалось отправить уведомление в группу")


# --- сборка приложения ------------------------------------------------------


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except ApiError as exc:
        return web.json_response({"ok": False, "error": exc.message}, status=exc.status)
    except Exception:
        logger.exception("Ошибка в обработчике %s", request.path)
        return web.json_response(
            {"ok": False, "error": "Внутренняя ошибка сервера"}, status=500
        )


def build_app(bot: Bot, config: Config) -> web.Application:
    app = web.Application(middlewares=[error_middleware])
    app["bot"] = bot
    app["config"] = config

    app.router.add_get("/", handle_ping)
    app.router.add_get("/app", handle_app)
    app.router.add_post("/api/state", handle_state)
    app.router.add_post("/api/publish", handle_publish)
    app.router.add_post("/api/schedule", handle_schedule)
    app.router.add_post("/api/cancel", handle_cancel)
    app.router.add_post("/api/reject", handle_reject)
    return app


async def start_web_server(bot: Bot, config: Config) -> None:
    runner = web.AppRunner(build_app(bot, config))
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=config.port)
    await site.start()
    logger.info("Веб-сервер и админ-панель запущены на порту %s", config.port)
