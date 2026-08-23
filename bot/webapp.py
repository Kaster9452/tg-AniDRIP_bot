"""HTTP-часть: раздача админ-панели и API для неё.

Каждый запрос от панели проходит две проверки:
1) подпись Telegram (initData) — что запрос действительно из Mini App;
2) права в группе админов — что этот пользователь вправе публиковать.

Вторую проверку нельзя опустить: подпись подтверждает лишь, кто перед нами,
но не то, что этому человеку что-то позволено.
"""

from __future__ import annotations

import base64
import html
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiohttp import web

from bot import database as db
from bot.config import Config
from bot.publisher import PublishError, publish_post
from bot.slots import (
    DAY_NAMES,
    OWN_TEXT_LIMIT,
    SLOT_HOURS,
    build_days,
    next_free_slot,
    resolve_slot,
)
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


def avatar_color(user_id: int) -> str:
    """Генерируем стабильный цвет аватарки на основе user_id."""
    colors = [
        "#FF5C9E",  # rose
        "#FFD166",  # citrus
        "#5BE9B9",  # mint
        "#7F8FFF",  # blue
        "#FF8A65",  # orange
        "#BA68C8",  # purple
        "#4DB6AC",  # teal
        "#E57373",  # red
    ]
    return colors[user_id % len(colors)]


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
        "userId": post["user_id"],
        "author": author_of(post),
        "initials": initials_of(post),
        "avatarColor": avatar_color(post["user_id"]),
        "preview": preview_of(post),
        "media": MEDIA_LABELS.get(post["content_type"]),
        "hasPhoto": post["content_type"] == "photo" and bool(post["file_id"]),
        "hasMedia": bool(post["file_id"]),
        "mediaType": post["content_type"],
        "ago": relative_ago(created, now_local),
        "submittedTime": created.strftime("%H:%M"),
        "submittedDay": day_label(created, now_local),
        "signed": bool(post["with_attribution"]),
        "own": bool(post["is_own"]),
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


def serialize_person(row, tz: ZoneInfo, now_local: datetime) -> dict:
    handle = f"@{row['username']}" if row["username"] else (row["name"] or "без имени")
    return {
        "userId": row["user_id"],
        "name": row["name"] or "",
        "handle": handle,
        "initials": (row["username"] or row["name"] or "?").lstrip("@")[:2].lower(),
        "avatarColor": avatar_color(row["user_id"]),
        "total": row["total"],
        "published": row["published"],
        "pending": row["pending"],
        "lastSeen": relative_ago(row["last_seen"].astimezone(tz), now_local),
        "banned": bool(row["banned"]),
        "banReason": row["ban_reason"] or "",
    }


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
    # сетка нужна и кнопке «Свой пост», и выбору времени в карточках
    days = build_days(tz, now_local, queue)

    return web.json_response(
        {
            "stats": stats,
            "inbox": [serialize(p, tz, now_local) for p in pending],
            "queue": [serialize(p, tz, now_local) for p in queue],
            "log": [serialize(p, tz, now_local) for p in published],
            "days": days,
            "nextSlot": next_free_slot(days),
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


async def handle_photo(request: web.Request) -> web.Response:
    """Отдаёт фото поста как data-URL, чтобы панель могла показать превью.

    Прямая ссылка на файл Telegram содержит токен бота, поэтому качаем файл
    сами и отдаём его панели уже в виде байтов — токен наружу не уходит.
    """
    payload = await read_payload(request)
    await authorize(request, payload)

    bot: Bot = request.app["bot"]
    post = await load_post(post_id_from(payload))

    if post["content_type"] != "photo" or not post["file_id"]:
        raise ApiError("У этого поста нет фото", status=404)

    try:
        file = await bot.get_file(post["file_id"])
        buffer = await bot.download_file(file.file_path)
    except Exception as exc:
        logger.exception("Не удалось скачать фото поста %s", post["id"])
        raise ApiError("Не удалось загрузить фото", status=502) from exc

    encoded = base64.b64encode(buffer.read()).decode("ascii")
    return web.json_response({"ok": True, "dataUrl": f"data:image/jpeg;base64,{encoded}"})


MEDIA_MIME_TYPES = {
    "photo": "image/jpeg",
    "animation": "image/gif",
    "sticker": "image/webp",
    "video": "video/mp4",
    "video_note": "video/mp4",
    "document": "application/octet-stream",
    "audio": "audio/mpeg",
    "voice": "audio/ogg",
}


async def handle_media(request: web.Request) -> web.Response:
    """Отдаёт медиа поста для предпросмотра без раскрытия токена бота."""
    payload = await read_payload(request)
    await authorize(request, payload)

    bot: Bot = request.app["bot"]
    post = await load_post(post_id_from(payload))
    file_id = post["file_id"]
    if not file_id:
        raise ApiError("У этого поста нет файла", status=404)

    try:
        file = await bot.get_file(file_id)
        buffer = await bot.download_file(file.file_path)
    except Exception as exc:
        logger.exception("Не удалось скачать медиа поста %s", post["id"])
        raise ApiError("Не удалось загрузить медиа", status=502) from exc

    encoded = base64.b64encode(buffer.read()).decode("ascii")
    mime = MEDIA_MIME_TYPES.get(post["content_type"], "application/octet-stream")
    return web.json_response({"ok": True, "dataUrl": f"data:{mime};base64,{encoded}"})


# Аватарки меняются редко, а на каждый запрос панели их десятки — поэтому
# держим уже скачанные в памяти. Кэш живёт до перезапуска сервиса.
_avatar_cache: dict[int, str | None] = {}


async def fetch_avatar(bot: Bot, user_id: int) -> str | None:
    """Аватарка пользователя как data-URL, или None если её нет.

    Как и с фото постов, прямая ссылка на файл Telegram содержит токен бота,
    поэтому качаем сами. Отсутствие аватарки — обычное дело: человек мог её
    не ставить или закрыть настройками приватности.
    """
    if user_id in _avatar_cache:
        return _avatar_cache[user_id]

    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if not photos.photos:
            _avatar_cache[user_id] = None
            return None
        # Размеры идут от меньшего к большему. Самый маленький бывает
        # совсем крошечным и мылит на экранах телефонов, поэтому берём
        # первый размер от 160px, а если таких нет — самый большой.
        sizes = photos.photos[0]
        chosen = next((s for s in sizes if s.width >= 160), sizes[-1])
        file = await bot.get_file(chosen.file_id)
        buffer = await bot.download_file(file.file_path)
    except Exception:
        logger.debug("Не удалось получить аватарку пользователя %s", user_id)
        _avatar_cache[user_id] = None
        return None

    encoded = base64.b64encode(buffer.read()).decode("ascii")
    url = f"data:image/jpeg;base64,{encoded}"
    _avatar_cache[user_id] = url
    return url


async def handle_avatars(request: web.Request) -> web.Response:
    """Панель присылает список ID разом — так меньше запросов, чем по одному."""
    payload = await read_payload(request)
    await authorize(request, payload)

    bot: Bot = request.app["bot"]

    raw = payload.get("userIds")
    if not isinstance(raw, list):
        raise ApiError("Не указаны пользователи")

    user_ids: list[int] = []
    for item in raw[:60]:
        try:
            user_ids.append(int(item))
        except (TypeError, ValueError):
            continue

    avatars: dict[str, str] = {}
    for user_id in dict.fromkeys(user_ids):
        url = await fetch_avatar(bot, user_id)
        if url:
            avatars[str(user_id)] = url

    return web.json_response({"avatars": avatars})


async def handle_people(request: web.Request) -> web.Response:
    payload = await read_payload(request)
    await authorize(request, payload)

    config: Config = request.app["config"]
    tz = config.timezone
    now_local = datetime.now(tz)

    query = str(payload.get("query", ""))[:64]
    only_banned = bool(payload.get("onlyBanned"))

    rows = await db.search_people(query, only_banned=only_banned)
    return web.json_response(
        {"people": [serialize_person(r, tz, now_local) for r in rows]}
    )


async def handle_ban(request: web.Request) -> web.Response:
    payload = await read_payload(request)
    admin = await authorize(request, payload)

    bot: Bot = request.app["bot"]

    by = f"@{admin['username']}" if admin.get("username") else admin.get("first_name", "админ")
    reason = str(payload.get("reason", "")).strip() or None

    # С карточки предложки приходит номер поста, с экрана людей — ID человека.
    post = None
    if payload.get("postId") is not None:
        post = await load_post(post_id_from(payload))
        user_id = post["user_id"]
        username, name = post["author_username"], post["author_name"]
    else:
        try:
            user_id = int(payload["userId"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiError("Не указан пользователь") from exc
        person = await db.get_person(user_id)
        username = person["username"] if person else None
        name = person["name"] if person else None

    await db.ban_user(user_id, username, name, reason, by)
    dropped = await db.reject_pending_by_user(user_id)
    if post is not None:
        await clear_admin_buttons(bot, post)

    message = "Заблокирован"
    if dropped > 1:
        message += f", постов убрано: {dropped}"
    return web.json_response({"ok": True, "message": message})


async def handle_unban(request: web.Request) -> web.Response:
    payload = await read_payload(request)
    await authorize(request, payload)

    try:
        user_id = int(payload["userId"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError("Не указан пользователь") from exc

    if not await db.unban_user(user_id):
        raise ApiError("Этот пользователь не заблокирован")

    return web.json_response({"ok": True, "message": "Блокировка снята"})


async def handle_slots(request: web.Request) -> web.Response:
    """Сетка на три дня для экрана «Свой пост»."""
    payload = await read_payload(request)
    await authorize(request, payload)

    config: Config = request.app["config"]
    tz = config.timezone
    now_local = datetime.now(tz)

    days = build_days(tz, now_local, await db.get_scheduled_posts())
    return web.json_response(
        {"days": days, "nextSlot": next_free_slot(days), "timezone": config.timezone_name}
    )


async def handle_own(request: web.Request) -> web.Response:
    """Админ пишет пост сам и ставит его в выбранный слот."""
    payload = await read_payload(request)
    admin = await authorize(request, payload)

    config: Config = request.app["config"]
    bot: Bot = request.app["bot"]
    tz = config.timezone
    now_local = datetime.now(tz)

    text = str(payload.get("text", "")).strip()
    if not text:
        raise ApiError("Напишите текст поста")
    if len(text) > OWN_TEXT_LIMIT:
        raise ApiError(f"Слишком длинный текст: максимум {OWN_TEXT_LIMIT} символов")

    try:
        day_offset = int(payload.get("day", 0))
    except (TypeError, ValueError) as exc:
        raise ApiError("Не выбран день") from exc
    if day_offset not in range(len(DAY_NAMES)):
        raise ApiError("Не выбран день")

    raw_time = str(payload.get("time", ""))
    try:
        hour = int(raw_time.split(":")[0])
    except (ValueError, IndexError) as exc:
        raise ApiError("Не выбрано время") from exc
    if hour not in SLOT_HOURS:
        raise ApiError("Такого слота нет в сетке")

    when = resolve_slot(tz, now_local, day_offset, hour)
    if when <= now_local:
        raise ApiError("Это время уже прошло")

    # Бот публикует с parse_mode=HTML, поэтому текст админа экранируем:
    # иначе любой символ < свалит отправку.
    content_html = html.escape(text)

    post_id = await db.create_own_post(
        user_id=int(admin["id"]),
        author_name=admin.get("first_name"),
        author_username=admin.get("username"),
        content_html=content_html,
        scheduled_at=when.astimezone(timezone.utc),
    )

    await notify_group(
        bot,
        config,
        f"🕓 Свой пост #{post_id} поставлен на {format_when(when, tz)} "
        f"({author_of_admin(admin)}).",
    )

    return web.json_response(
        {
            "ok": True,
            "message": f"Пост #{post_id} встанет в {hour:02d}:00, {DAY_NAMES[day_offset]}",
        }
    )


def author_of_admin(admin: dict) -> str:
    if admin.get("username"):
        return f"@{admin['username']}"
    return admin.get("first_name") or f"ID {admin.get('id')}"


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
    app.router.add_post("/api/photo", handle_photo)
    app.router.add_post("/api/media", handle_media)
    app.router.add_post("/api/avatars", handle_avatars)
    app.router.add_post("/api/slots", handle_slots)
    app.router.add_post("/api/own", handle_own)
    app.router.add_post("/api/publish", handle_publish)
    app.router.add_post("/api/schedule", handle_schedule)
    app.router.add_post("/api/cancel", handle_cancel)
    app.router.add_post("/api/reject", handle_reject)
    app.router.add_post("/api/people", handle_people)
    app.router.add_post("/api/ban", handle_ban)
    app.router.add_post("/api/unban", handle_unban)
    return app


async def start_web_server(bot: Bot, config: Config) -> None:
    runner = web.AppRunner(build_app(bot, config))
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=config.port)
    await site.start()
    logger.info("Веб-сервер и админ-панель запущены на порту %s", config.port)
