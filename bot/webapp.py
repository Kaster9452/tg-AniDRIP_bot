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
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import BufferedInputFile, InputMediaPhoto, InputMediaVideo
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

# Ограничения на файлы своего поста: панель шлёт их через base64 в JSON,
# поэтому держим лимиты скромными, чтобы не раздуть память сервера.
OWN_FILES_LIMIT = 10
OWN_FILE_MAX_BYTES = 15 * 1024 * 1024
OWN_FILES_MAX_TOTAL_BYTES = 60 * 1024 * 1024

# Telegram ограничивает подписи к медиа 1024 символами — это меньше,
# чем лимит обычного текстового сообщения.
CAPTION_TEXT_LIMIT = 1024


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _decode_data_url(data_url: str) -> bytes | None:
    """Разбирает data:...;base64,... в сырые байты файла."""
    if not isinstance(data_url, str) or "," not in data_url:
        return None
    header, _, encoded = data_url.partition(",")
    if "base64" not in header:
        return None
    try:
        return base64.b64decode(encoded)
    except (ValueError, TypeError):
        return None


def _decode_files_payload(raw_files: object) -> list[tuple[str, bytes]]:
    """Разбирает и проверяет список файлов, присланных панелью как base64."""
    if not isinstance(raw_files, list):
        raise ApiError("Некорректный список файлов")

    items: list[tuple[str, bytes]] = []
    total_bytes = 0
    for entry in raw_files:
        if not isinstance(entry, dict):
            raise ApiError("Некорректный файл")
        kind = entry.get("type")
        if kind not in ("photo", "video"):
            raise ApiError("Поддерживаются только фото и видео")
        raw = _decode_data_url(str(entry.get("dataUrl", "")))
        if not raw:
            raise ApiError("Не удалось прочитать один из файлов")
        if len(raw) > OWN_FILE_MAX_BYTES:
            raise ApiError(
                f"Файл слишком большой: максимум {OWN_FILE_MAX_BYTES // (1024 * 1024)} МБ"
            )
        total_bytes += len(raw)
        if total_bytes > OWN_FILES_MAX_TOTAL_BYTES:
            raise ApiError(
                f"Суммарный размер файлов больше {OWN_FILES_MAX_TOTAL_BYTES // (1024 * 1024)} МБ"
            )
        items.append((kind, raw))
    return items


async def _mint_file_ids(
    bot: Bot, chat_id: int, items: list[tuple[str, bytes]], caption: str | None
) -> list[dict]:
    """Отправляет сырые файлы в Telegram, чтобы получить их file_id.

    Публикация хранит только file_id, а не байты, поэтому любой новый файл
    сперва нужно один раз отправить ботом — сюда, в группу админов.
    """
    if not items:
        return []

    if len(items) == 1:
        kind, raw = items[0]
        input_file = BufferedInputFile(
            raw, filename="photo.jpg" if kind == "photo" else "video.mp4"
        )
        if kind == "photo":
            sent = await bot.send_photo(chat_id, input_file, caption=caption)
            file_id = sent.photo[-1].file_id
            thumb_id = None
        else:
            sent = await bot.send_video(chat_id, input_file, caption=caption)
            file_id = sent.video.file_id
            thumbnail = getattr(sent.video, "thumbnail", None) or getattr(sent.video, "thumb", None)
            thumb_id = getattr(thumbnail, "file_id", None)
        return [{"type": kind, "file_id": file_id, "thumb": thumb_id}]

    media = []
    for index, (kind, raw) in enumerate(items):
        input_file = BufferedInputFile(
            raw, filename=f"{kind}{index}.{'jpg' if kind == 'photo' else 'mp4'}"
        )
        cls = InputMediaVideo if kind == "video" else InputMediaPhoto
        media.append(cls(media=input_file, caption=caption if index == 0 else None))
    sent_list = await bot.send_media_group(chat_id, media=media)

    result = []
    for (kind, _), sent in zip(items, sent_list):
        if kind == "photo":
            fid = sent.photo[-1].file_id
            thumb_id = None
        else:
            fid = sent.video.file_id
            thumbnail = getattr(sent.video, "thumbnail", None) or getattr(sent.video, "thumb", None)
            thumb_id = getattr(thumbnail, "file_id", None)
        result.append({"type": kind, "file_id": fid, "thumb": thumb_id})
    return result


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
    album_count = None
    album_types = None
    first_type = post["content_type"]
    thumb_id = post["media_thumb_id"]
    first_fid = post["file_id"]

    if post["media_group"]:
        try:
            album_items = json.loads(post["media_group"])
            album_count = len(album_items)
            album_types = [item.get("type", "photo") for item in album_items]
            if album_items:
                first_type = album_items[0].get("type", first_type)
                first_fid = album_items[0].get("file_id", first_fid)
                if not thumb_id:
                    thumb_id = album_items[0].get("thumb")
        except (TypeError, ValueError):
            album_count = None
            album_types = None

    has_photo = (first_type == "photo" and bool(first_fid))
    has_media_thumb = bool(thumb_id) or (first_type in ("video", "video_note", "animation") and bool(first_fid))
    has_media = bool(post["file_id"]) or bool(album_count)

    data = {
        "id": post["id"],
        "userId": post["user_id"],
        "author": author_of(post),
        "initials": initials_of(post),
        "avatarColor": avatar_color(post["user_id"]),
        "preview": preview_of(post),
        "text": (post["content_html"] or "").strip(),
        "media": MEDIA_LABELS.get(first_type, MEDIA_LABELS.get(post["content_type"])),
        "albumCount": album_count,
        "albumTypes": album_types,
        "hasPhoto": has_photo,
        "hasMedia": has_media,
        "mediaType": first_type,
        "hasMediaThumb": has_media_thumb,
        "ago": relative_ago(created, now_local),
        "submittedTime": created.strftime("%H:%M"),
        "submittedDay": day_label(created, now_local),
        "signed": bool(post["with_attribution"]),
        "own": bool(post["is_own"]),
        "scheduledBy": post["scheduled_by"] or "",
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
    subscriber_history = await db.get_subscriber_count_history(
        (now_local - timedelta(days=30)).astimezone(timezone.utc)
    )
    subscriber_stats = None
    if subscriber_history:
        latest = subscriber_history[-1]
        first = subscriber_history[0]
        today_history = [
            row
            for row in subscriber_history
            if row["captured_at"] >= day_start.astimezone(timezone.utc)
        ]
        today_first = today_history[0] if today_history else latest
        subscriber_stats = {
            "current": latest["subscriber_count"],
            "change": latest["subscriber_count"] - first["subscriber_count"],
            "changeToday": latest["subscriber_count"]
            - today_first["subscriber_count"],
            "sampledAt": latest["captured_at"].astimezone(tz).isoformat(),
            "history": [
                {
                    "at": row["captured_at"].astimezone(tz).isoformat(),
                    "count": row["subscriber_count"],
                }
                for row in subscriber_history
            ],
        }
    # сетка нужна и кнопке «Свой пост», и выбору времени в карточках
    days = build_days(tz, now_local, queue)

    return web.json_response(
        {
            "stats": stats,
            "subscribers": subscriber_stats,
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
    admin = await authorize(request, payload)

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
    await db.mark_scheduled(
        post["id"], when.astimezone(timezone.utc), signed, author_of_admin(admin)
    )
    await set_scheduled_buttons(bot, post)

    return web.json_response(
        {"ok": True, "message": f"Запланировано на {format_when(when, tz)}"}
    )


async def handle_cancel(request: web.Request) -> web.Response:
    payload = await read_payload(request)
    admin = await authorize(request, payload)

    config: Config = request.app["config"]
    bot: Bot = request.app["bot"]
    post = await load_post(post_id_from(payload))
    if post["status"] != "scheduled":
        raise ApiError("Этот пост не запланирован")

    await db.mark_cancelled(post["id"])
    await restore_admin_buttons(bot, post)
    
    post_type_label = "Свой пост" if post.get("is_own") else "Пост"
    await notify_group(
        bot,
        config,
        f"🗑 {post_type_label} #{post['id']} снят с отложки ({author_of_admin(admin)})."
    )
    
    return web.json_response({"ok": True, "message": "Снято с публикации"})


async def handle_edit_post(request: web.Request) -> web.Response:
    """Меняет текст, состав файлов и, при желании, время уже отложенного поста."""
    payload = await read_payload(request)
    admin = await authorize(request, payload)

    bot: Bot = request.app["bot"]
    config: Config = request.app["config"]
    tz = config.timezone

    post = await load_post(post_id_from(payload))
    if post["status"] != "scheduled":
        raise ApiError("Редактировать можно только отложенные посты")

    existing: list[dict] = []
    if post["media_group"]:
        existing = json.loads(post["media_group"])
    elif post["file_id"]:
        existing = [{"type": post["content_type"], "file_id": post["file_id"]}]

    raw_keep = payload.get("keepIndexes")
    if raw_keep is None:
        keep_indexes = set(range(len(existing)))
    elif isinstance(raw_keep, list):
        try:
            keep_indexes = {int(i) for i in raw_keep}
        except (TypeError, ValueError) as exc:
            raise ApiError("Некорректный список файлов") from exc
    else:
        raise ApiError("Некорректный список файлов")

    kept_items = [item for i, item in enumerate(existing) if i in keep_indexes]
    new_raw_items = _decode_files_payload(payload.get("newFiles") or [])

    if len(kept_items) + len(new_raw_items) > OWN_FILES_LIMIT:
        raise ApiError(f"Не больше {OWN_FILES_LIMIT} файлов в одном посте")

    text = str(payload.get("text", "")).strip()
    if not text and not kept_items and not new_raw_items:
        raise ApiError("Нужен текст или хотя бы один файл")

    limit = OWN_TEXT_LIMIT if not (kept_items or new_raw_items) else CAPTION_TEXT_LIMIT
    if len(text) > limit:
        raise ApiError(f"Слишком длинный текст: максимум {limit} символов")

    raw_when = str(payload.get("when", "")).strip()
    when = None
    if raw_when:
        try:
            when = parse_when(raw_when, tz)
        except TimeParseError as exc:
            raise ApiError(str(exc)) from exc
        if when <= datetime.now(tz):
            raise ApiError("Это время уже прошло")

    content_html = html.escape(text)

    new_items: list[dict] = []
    if new_raw_items:
        try:
            new_items = await _mint_file_ids(
                bot, config.admin_group_id, new_raw_items, content_html or None
            )
        except Exception as exc:
            logger.exception("Не удалось загрузить новые файлы для поста %s", post["id"])
            raise ApiError(f"Не удалось загрузить файлы: {exc}", status=502) from exc

    final_items = kept_items + new_items

    if not final_items:
        content_type, file_id, media_thumb_id, media_group = "text", None, None, None
    else:
        content_type = final_items[0]["type"]
        file_id = final_items[0]["file_id"]
        if 0 in keep_indexes and kept_items:
            media_thumb_id = post["media_thumb_id"]
        else:
            media_thumb_id = final_items[0].get("thumb")
        media_group = final_items if len(final_items) > 1 else None

    await db.update_post_media(
        post["id"], content_html, content_type, file_id, media_thumb_id, media_group
    )
    if when is not None:
        await db.mark_scheduled(
            post["id"],
            when.astimezone(timezone.utc),
            post["with_attribution"],
            author_of_admin(admin),
        )
    return web.json_response({"ok": True, "message": "Изменения сохранены"})


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
    """Отдаёт медиа поста для предпросмотра без раскрытия токена бота.

    Если пост — альбом, параметр index выбирает конкретный элемент;
    без него отдаётся первый/единственный файл поста.
    """
    payload = await read_payload(request)
    await authorize(request, payload)

    bot: Bot = request.app["bot"]
    post = await load_post(post_id_from(payload))

    file_id = post["file_id"]
    content_type = post["content_type"]

    index = payload.get("index")
    if index is not None and post["media_group"]:
        items = json.loads(post["media_group"])
        try:
            item = items[int(index)]
        except (ValueError, IndexError):
            raise ApiError("Такого элемента альбома нет", status=404)
        file_id = item["file_id"]
        content_type = item["type"]

    if not file_id:
        raise ApiError("У этого поста нет файла", status=404)

    try:
        file = await bot.get_file(file_id)
        buffer = await bot.download_file(file.file_path)
    except Exception as exc:
        logger.exception("Не удалось скачать медиа поста %s", post["id"])
        raise ApiError("Не удалось загрузить медиа", status=502) from exc

    encoded = base64.b64encode(buffer.read()).decode("ascii")
    mime = MEDIA_MIME_TYPES.get(content_type, "application/octet-stream")
    return web.json_response({"ok": True, "dataUrl": f"data:{mime};base64,{encoded}"})


async def handle_media_thumb(request: web.Request) -> web.Response:
    """Отдаёт миниатюру медиа для быстрого предпросмотра."""
    payload = await read_payload(request)
    await authorize(request, payload)
    post = await load_post(post_id_from(payload))
    thumb_id = post["media_thumb_id"]
    if not thumb_id and post["media_group"]:
        try:
            items = json.loads(post["media_group"])
            if items:
                thumb_id = items[0].get("thumb")
        except Exception:
            pass

    bot: Bot = request.app["bot"]
    if thumb_id:
        try:
            file = await bot.get_file(thumb_id)
            buffer = await bot.download_file(file.file_path)
            encoded = base64.b64encode(buffer.read()).decode("ascii")
            return web.json_response({"ok": True, "dataUrl": f"data:image/jpeg;base64,{encoded}"})
        except Exception as exc:
            logger.exception("Не удалось скачать миниатюру поста %s", post["id"])

    # Если thumb_id нет, но это фото или первый элемент альбома — фото
    target_fid = post["file_id"]
    target_type = post["content_type"]
    if post["media_group"]:
        try:
            items = json.loads(post["media_group"])
            if items:
                target_fid = items[0].get("file_id", target_fid)
                target_type = items[0].get("type", target_type)
        except Exception:
            pass

    if target_type == "photo" and target_fid:
        try:
            file = await bot.get_file(target_fid)
            buffer = await bot.download_file(file.file_path)
            encoded = base64.b64encode(buffer.read()).decode("ascii")
            return web.json_response({"ok": True, "dataUrl": f"data:image/jpeg;base64,{encoded}"})
        except Exception as exc:
            logger.exception("Не удалось скачать фото поста %s", post["id"])

    raise ApiError("У этого медиа нет миниатюры", status=404)


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
    raw_files = payload.get("files") or []
    if not text and not raw_files:
        raise ApiError("Напишите текст или прикрепите фото/видео")
    if len(text) > OWN_TEXT_LIMIT:
        raise ApiError(f"Слишком длинный текст: максимум {OWN_TEXT_LIMIT} символов")
    if len(raw_files) > OWN_FILES_LIMIT:
        raise ApiError(f"Не больше {OWN_FILES_LIMIT} файлов в одном посте")

    items = _decode_files_payload(raw_files)

    raw_when = str(payload.get("when", "")).strip()
    if raw_when:
        try:
            when = parse_when(raw_when, tz)
        except TimeParseError as exc:
            raise ApiError(str(exc)) from exc
    else:
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

    content_type = "text"
    file_id: str | None = None
    media_thumb_id: str | None = None
    media_group: list[dict] | None = None

    if items:
        # Файл отправляется в группу админов, чтобы получить его Telegram file_id
        # для дальнейшей публикации — сами байты в базе не храним.
        try:
            minted = await _mint_file_ids(
                bot, config.admin_group_id, items, content_html or None
            )
        except Exception as exc:
            logger.exception("Не удалось отправить файлы своего поста")
            raise ApiError(f"Не удалось отправить файлы: {exc}", status=502) from exc
        file_id = minted[0]["file_id"]
        content_type = minted[0]["type"]
        media_thumb_id = minted[0].get("thumb")
        media_group = minted if len(minted) > 1 else None

    post_id = await db.create_own_post(
        user_id=int(admin["id"]),
        author_name=admin.get("first_name"),
        author_username=admin.get("username"),
        content_html=content_html,
        scheduled_at=when.astimezone(timezone.utc),
        content_type=content_type,
        file_id=file_id,
        media_thumb_id=media_thumb_id,
        media_group=media_group,
        scheduled_by=author_of_admin(admin),
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
            "message": f"Пост #{post_id} встанет на {format_when(when, tz)}",
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
    # Файлы своего поста приходят base64 в теле JSON, а у aiohttp по умолчанию лимит 1 МБ.
    app = web.Application(
        middlewares=[error_middleware], client_max_size=100 * 1024 * 1024
    )
    app["bot"] = bot
    app["config"] = config

    app.router.add_get("/", handle_ping)
    app.router.add_get("/app", handle_app)
    app.router.add_post("/api/state", handle_state)
    app.router.add_post("/api/photo", handle_photo)
    app.router.add_post("/api/media", handle_media)
    app.router.add_post("/api/media-thumb", handle_media_thumb)
    app.router.add_post("/api/avatars", handle_avatars)
    app.router.add_post("/api/slots", handle_slots)
    app.router.add_post("/api/own", handle_own)
    app.router.add_post("/api/publish", handle_publish)
    app.router.add_post("/api/schedule", handle_schedule)
    app.router.add_post("/api/cancel", handle_cancel)
    app.router.add_post("/api/edit-post", handle_edit_post)
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
