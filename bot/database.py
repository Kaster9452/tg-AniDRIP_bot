"""Работа с базой данных PostgreSQL (например, Neon).

SQLite не подходит: на бесплатном тарифе Render файловая система временная,
и база обнулялась бы при каждом перезапуске сервиса.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import asyncpg

_pool: asyncpg.Pool | None = None


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS message_map (
    admin_message_id BIGINT PRIMARY KEY,
    user_id          BIGINT NOT NULL,
    user_message_id  BIGINT
);

CREATE TABLE IF NOT EXISTS posts (
    id                 BIGSERIAL PRIMARY KEY,
    user_id            BIGINT NOT NULL,
    user_message_id    BIGINT NOT NULL,
    author_name        TEXT,
    author_username    TEXT,
    content_type       TEXT NOT NULL,
    content_html       TEXT,
    file_id            TEXT,
    media_thumb_id     TEXT,
    admin_chat_id      BIGINT,
    admin_message_id   BIGINT,
    status             TEXT NOT NULL DEFAULT 'pending',
    with_attribution   BOOLEAN NOT NULL DEFAULT FALSE,
    scheduled_at       TIMESTAMPTZ,
    published_at       TIMESTAMPTZ,
    channel_message_id BIGINT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_posts_due ON posts (status, scheduled_at);

CREATE TABLE IF NOT EXISTS subscriber_count_history (
    captured_at      TIMESTAMPTZ PRIMARY KEY,
    subscriber_count INTEGER NOT NULL CHECK (subscriber_count >= 0)
);

CREATE INDEX IF NOT EXISTS idx_subscriber_count_history_captured
    ON subscriber_count_history (captured_at DESC);

-- ALTER вместо переделки CREATE TABLE: таблица posts уже существует в базе
-- на Render, а CREATE TABLE IF NOT EXISTS новую колонку в неё не добавит.
ALTER TABLE posts ADD COLUMN IF NOT EXISTS file_id TEXT;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS media_thumb_id TEXT;

-- Альбом (несколько фото/видео одним сообщением): список {type, file_id}
-- в порядке отправки. NULL — обычный одиночный пост.
ALTER TABLE posts ADD COLUMN IF NOT EXISTS media_group TEXT;

-- Пост, написанный самим админом в панели, а не присланный пользователем.
-- Нужно, чтобы такие посты не попадали в список «Люди» и были подписаны иначе.
ALTER TABLE posts ADD COLUMN IF NOT EXISTS is_own BOOLEAN NOT NULL DEFAULT FALSE;

-- Кто из админов поставил пост в очередь — показывается в панели и в /queue.
ALTER TABLE posts ADD COLUMN IF NOT EXISTS scheduled_by TEXT;

-- Время записи нужно, чтобы периодически чистить старые связки message_map
-- (без него база бы бесконечно росла записью на каждое пересланное сообщение).
ALTER TABLE message_map ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE TABLE IF NOT EXISTS banned_users (
    user_id   BIGINT PRIMARY KEY,
    username  TEXT,
    name      TEXT,
    reason    TEXT,
    banned_by TEXT,
    banned_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Вечная личная статистика автора: живёт независимо от строк posts,
-- которые периодически чистятся (published/rejected/cancelled/failed
-- старше POSTS_RETENTION удаляются, счётчики людей — никогда).
CREATE TABLE IF NOT EXISTS user_stats (
    user_id         BIGINT PRIMARY KEY,
    author_name     TEXT,
    author_username TEXT,
    total           INTEGER NOT NULL DEFAULT 0,
    published       INTEGER NOT NULL DEFAULT 0,
    last_seen       TIMESTAMPTZ
);
"""


def _clean_dsn(database_url: str) -> tuple[str, Any]:
    """asyncpg не понимает часть параметров из строки подключения Neon
    (sslmode, channel_binding и т.п.), поэтому убираем их из URL и
    передаём настройку SSL отдельно."""
    ssl_setting: Any = "require"
    if "?" in database_url:
        base, query = database_url.split("?", 1)
        if "sslmode=disable" in query:
            ssl_setting = False
        return base, ssl_setting
    return database_url, ssl_setting


async def init_db(database_url: str) -> None:
    """Создаёт пул подключений и таблицы. Вызывается один раз при старте."""
    global _pool
    dsn, ssl_setting = _clean_dsn(database_url)
    _pool = await asyncpg.create_pool(
        dsn=dsn,
        ssl=ssl_setting,
        min_size=1,
        max_size=5,
        command_timeout=30,
    )
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
        # Разовый перенос накопленной статистики в user_stats (ON CONFLICT
        # DO NOTHING: при следующих стартах уже существующие строки не трогаем,
        # чтобы бэкфилл не задваивал счётчики поверх живых инкрементов).
        await conn.execute(
            """
            INSERT INTO user_stats (user_id, author_name, author_username,
                                    total, published, last_seen)
            SELECT p.user_id,
                   (array_agg(p.author_name     ORDER BY p.id DESC))[1],
                   (array_agg(p.author_username ORDER BY p.id DESC))[1],
                   COUNT(*),
                   COUNT(*) FILTER (WHERE p.status = 'published'),
                   MAX(p.created_at)
            FROM posts p
            WHERE NOT p.is_own
            GROUP BY p.user_id
            ON CONFLICT (user_id) DO NOTHING
            """
        )


async def close_db() -> None:
    if _pool is not None:
        await _pool.close()


def _require_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("База данных не инициализирована — вызовите init_db()")
    return _pool


# --- связка сообщений для ответов админов ------------------------------------


async def save_mapping(
    admin_message_id: int, user_id: int, user_message_id: int | None = None
) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO message_map (admin_message_id, user_id, user_message_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (admin_message_id) DO UPDATE
                SET user_id = EXCLUDED.user_id,
                    user_message_id = EXCLUDED.user_message_id
            """,
            admin_message_id,
            user_id,
            user_message_id,
        )


async def get_user_id(admin_message_id: int) -> int | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT user_id FROM message_map WHERE admin_message_id = $1",
            admin_message_id,
        )


async def purge_old_message_map(cutoff: datetime) -> int:
    """Удаляет связки старше cutoff — без этого таблица растёт на каждое
    пересланное сообщение (включая каждое фото альбома) и никогда не убывает."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM message_map WHERE created_at < $1", cutoff
        )
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


async def purge_old_posts(cutoff: datetime) -> int:
    """Удаляет отработанные посты старше cutoff: опубликованные, отклонённые,
    снятые и упавшие. Живые (pending/scheduled) не трогает никогда.

    Личная статистика людей живёт в user_stats и от этих строк не зависит,
    история подписчиков — отдельная таблица, тоже не затрагивается."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM posts
             WHERE status IN ('published', 'rejected', 'cancelled', 'failed')
               AND COALESCE(published_at, created_at) < $1
            """,
            cutoff,
        )
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


# --- предложенные посты -------------------------------------------------------


async def create_post(
    user_id: int,
    user_message_id: int,
    author_name: str | None,
    author_username: str | None,
    content_type: str,
    content_html: str | None,
    file_id: str | None = None,
    media_thumb_id: str | None = None,
    media_group: list[dict] | None = None,
) -> int:
    pool = _require_pool()
    async with pool.acquire() as conn:
        post_id = await conn.fetchval(
            """
            INSERT INTO posts (user_id, user_message_id, author_name,
                               author_username, content_type, content_html, file_id,
                               media_thumb_id, media_group)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id
            """,
            user_id,
            user_message_id,
            author_name,
            author_username,
            content_type,
            content_html,
            file_id,
            media_thumb_id,
            json.dumps(media_group) if media_group else None,
        )
        # Вечная статистика автора: «прислал» растёт при каждом новом посте
        await conn.execute(
            """
            INSERT INTO user_stats (user_id, author_name, author_username,
                                    total, published, last_seen)
            VALUES ($1, $2, $3, 1, 0, NOW())
            ON CONFLICT (user_id) DO UPDATE
               SET total           = user_stats.total + 1,
                   author_name     = EXCLUDED.author_name,
                   author_username = EXCLUDED.author_username,
                   last_seen       = NOW()
            """,
            user_id,
            author_name,
            author_username,
        )
        return post_id


async def create_own_post(
    user_id: int,
    author_name: str | None,
    author_username: str | None,
    content_html: str,
    scheduled_at: datetime,
    content_type: str = "text",
    file_id: str | None = None,
    media_thumb_id: str | None = None,
    media_group: list[dict] | None = None,
    scheduled_by: str | None = None,
) -> int:
    """Пост, который админ написал сам в панели, сразу встаёт в очередь.

    user_message_id = 0: исходного сообщения в личке не существует, а при
    публикации текстового поста оно и не нужно — берётся content_html.
    """
    pool = _require_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO posts (user_id, user_message_id, author_name, author_username,
                               content_type, content_html, file_id, media_thumb_id, media_group, status,
                               scheduled_at, with_attribution, is_own, scheduled_by)
            VALUES ($1, 0, $2, $3, $4, $5, $6, $7, $8, 'scheduled', $9, FALSE, TRUE, $10)
            RETURNING id
            """,
            user_id,
            author_name,
            author_username,
            content_type,
            content_html,
            file_id,
            media_thumb_id,
            json.dumps(media_group) if media_group else None,
            scheduled_at,
            scheduled_by,
        )


async def attach_admin_message(post_id: int, chat_id: int, message_id: int) -> None:
    """Запоминает, где в группе висит сообщение с кнопками для этого поста."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE posts SET admin_chat_id = $2, admin_message_id = $3 WHERE id = $1",
            post_id,
            chat_id,
            message_id,
        )


async def get_post(post_id: int) -> asyncpg.Record | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)


async def mark_published(
    post_id: int, channel_message_id: int, with_attribution: bool
) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE posts
               SET status = 'published',
                   published_at = NOW(),
                   channel_message_id = $2,
                   with_attribution = $3,
                   scheduled_at = NULL
             WHERE id = $1
            """,
            post_id,
            channel_message_id,
            with_attribution,
        )
        # Вечная статистика: «в канале» растёт только у постов от людей
        # (свои посты админа в статистике «Люди» не участвуют, как и раньше)
        await conn.execute(
            """
            UPDATE user_stats
               SET published = published + 1
             WHERE user_id = (SELECT user_id FROM posts WHERE id = $1 AND NOT is_own)
            """,
            post_id,
        )


async def mark_scheduled(
    post_id: int,
    scheduled_at: datetime,
    with_attribution: bool,
    scheduled_by: str | None = None,
) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE posts
               SET status = 'scheduled',
                   scheduled_at = $2,
                   with_attribution = $3,
                   scheduled_by = $4
             WHERE id = $1
            """,
            post_id,
            scheduled_at,
            with_attribution,
            scheduled_by,
        )


async def mark_cancelled(post_id: int) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE posts SET status = 'cancelled', scheduled_at = NULL WHERE id = $1",
            post_id,
        )


async def update_post_media(
    post_id: int,
    content_html: str,
    content_type: str,
    file_id: str | None,
    media_thumb_id: str | None,
    media_group: list[dict] | None,
) -> None:
    """Меняет текст и состав файлов уже отложенного поста без пересылки заново."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE posts
               SET content_html = $2,
                   content_type = $3,
                   file_id = $4,
                   media_thumb_id = $5,
                   media_group = $6
             WHERE id = $1
            """,
            post_id,
            content_html,
            content_type,
            file_id,
            media_thumb_id,
            json.dumps(media_group) if media_group else None,
        )


async def mark_failed(post_id: int) -> None:
    """Пост не удалось опубликовать — убираем из очереди, чтобы планировщик
    не пытался снова и снова."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE posts SET status = 'failed', scheduled_at = NULL WHERE id = $1",
            post_id,
        )


async def get_due_posts(now: datetime) -> list[asyncpg.Record]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT * FROM posts
             WHERE status = 'scheduled' AND scheduled_at <= $1
             ORDER BY scheduled_at
            """,
            now,
        )


async def get_scheduled_posts() -> list[asyncpg.Record]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM posts WHERE status = 'scheduled' ORDER BY scheduled_at"
        )


async def get_pending_posts(limit: int = 20) -> list[asyncpg.Record]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM posts WHERE status = 'pending' ORDER BY id DESC LIMIT $1",
            limit,
        )


async def get_published_posts(limit: int = 10) -> list[asyncpg.Record]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT * FROM posts
             WHERE status = 'published'
             ORDER BY published_at DESC
             LIMIT $1
            """,
            limit,
        )

async def mark_rejected(post_id: int) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE posts SET status = 'rejected', scheduled_at = NULL WHERE id = $1",
            post_id,
        )


async def count_stats(day_start_utc: datetime) -> dict[str, int]:
    """Числа для шапки панели: ждут решения, в очереди, ушло за сегодня."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'pending')   AS pending,
                COUNT(*) FILTER (WHERE status = 'scheduled') AS scheduled,
                COUNT(*) FILTER (WHERE status = 'published'
                                   AND published_at >= $1)   AS today
            FROM posts
            """,
            day_start_utc,
        )
    return {
        "pending": row["pending"] or 0,
        "scheduled": row["scheduled"] or 0,
        "today": row["today"] or 0,
    }


async def record_subscriber_count(
    subscriber_count: int, captured_at: datetime | None = None
) -> None:
    """Сохраняет снимок размера канала для последующего сравнения."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO subscriber_count_history (captured_at, subscriber_count)
            VALUES (COALESCE($2, NOW()), $1)
            ON CONFLICT (captured_at) DO UPDATE
                SET subscriber_count = EXCLUDED.subscriber_count
            """,
            subscriber_count,
            captured_at,
        )


async def get_subscriber_count_history(since: datetime) -> list[asyncpg.Record]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT captured_at, subscriber_count
              FROM subscriber_count_history
             WHERE captured_at >= $1
             ORDER BY captured_at
            """,
            since,
        )


# --- блокировки ---------------------------------------------------------------
# Telegram не позволяет боту запретить человеку писать ему, поэтому список
# блокировок ведём сами: сообщения из него просто не пересылаются админам.


async def ban_user(
    user_id: int,
    username: str | None,
    name: str | None,
    reason: str | None,
    banned_by: str | None,
) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO banned_users (user_id, username, name, reason, banned_by)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (user_id) DO UPDATE
                SET username = EXCLUDED.username,
                    name = EXCLUDED.name,
                    reason = EXCLUDED.reason,
                    banned_by = EXCLUDED.banned_by,
                    banned_at = NOW()
            """,
            user_id,
            username,
            name,
            reason,
            banned_by,
        )


async def unban_user(user_id: int) -> bool:
    """Возвращает True, если человек действительно был в списке."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM banned_users WHERE user_id = $1", user_id
        )
    return result.endswith("1")


async def is_banned(user_id: int) -> bool:
    pool = _require_pool()
    async with pool.acquire() as conn:
        return bool(
            await conn.fetchval(
                "SELECT 1 FROM banned_users WHERE user_id = $1", user_id
            )
        )


async def get_banned_users() -> list[asyncpg.Record]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM banned_users ORDER BY banned_at DESC"
        )


async def reject_pending_by_user(user_id: int) -> int:
    """Заодно убирает из очереди всё, что человек уже успел прислать."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE posts SET status = 'rejected', scheduled_at = NULL
             WHERE user_id = $1 AND status IN ('pending', 'scheduled')
            """,
            user_id,
        )
    try:
        return int(result.rsplit(" ", 1)[-1])
    except ValueError:
        return 0


async def get_post_by_user_message(user_id: int) -> asyncpg.Record | None:
    """Последний пост этого человека — нужен, чтобы узнать его имя и
    username при блокировке через ответ в группе."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM posts WHERE user_id = $1 ORDER BY id DESC LIMIT 1",
            user_id,
        )


# --- люди -------------------------------------------------------------------
# Список подписчиков канала Telegram боту не отдаёт, поэтому «люди» — это все,
# кто когда-либо писал боту: их мы знаем из таблицы постов.


async def search_people(
    query: str = "", only_banned: bool = False, limit: int = 60
) -> list[asyncpg.Record]:
    """Ищет по username, имени или числовому ID. Пустой запрос — все подряд,
    начиная с тех, кто писал недавно.

    Вечные счётчики (total, published) и имя берутся из user_stats — они
    не зависят от чистки старых постов. «Ждёт» — живой подсчёт по posts,
    где всегда лежат только актуальные посты.
    """
    pattern = f"%{query.strip()}%" if query.strip() else None
    numeric = query.strip() if query.strip().isdigit() else None

    pool = _require_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT
                s.user_id,
                s.author_name                             AS name,
                s.author_username                         AS username,
                s.total,
                s.published,
                (SELECT COUNT(*) FROM posts p
                  WHERE p.user_id = s.user_id
                    AND p.status = 'pending')             AS pending,
                s.last_seen,
                (b.user_id IS NOT NULL)                   AS banned,
                b.reason                                  AS ban_reason
            FROM user_stats s
            LEFT JOIN banned_users b ON b.user_id = s.user_id
            WHERE ($1::text IS NULL
                   OR s.author_username ILIKE $1
                   OR s.author_name ILIKE $1
                   OR s.user_id::text = $2)
              AND ($3 = FALSE OR b.user_id IS NOT NULL)
            ORDER BY s.last_seen DESC NULLS LAST
            LIMIT $4
            """,
            pattern,
            numeric,
            only_banned,
            limit,
        )


async def get_person(user_id: int) -> asyncpg.Record | None:
    rows = await search_people(str(user_id), limit=1)
    return rows[0] if rows else None
