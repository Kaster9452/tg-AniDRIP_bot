"""Работа с базой данных PostgreSQL (например, Neon).

SQLite не подходит: на бесплатном тарифе Render файловая система временная,
и база обнулялась бы при каждом перезапуске сервиса.
"""

from __future__ import annotations

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


# --- предложенные посты -------------------------------------------------------


async def create_post(
    user_id: int,
    user_message_id: int,
    author_name: str | None,
    author_username: str | None,
    content_type: str,
    content_html: str | None,
) -> int:
    pool = _require_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO posts (user_id, user_message_id, author_name,
                               author_username, content_type, content_html)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            user_id,
            user_message_id,
            author_name,
            author_username,
            content_type,
            content_html,
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


async def mark_scheduled(
    post_id: int, scheduled_at: datetime, with_attribution: bool
) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE posts
               SET status = 'scheduled',
                   scheduled_at = $2,
                   with_attribution = $3
             WHERE id = $1
            """,
            post_id,
            scheduled_at,
            with_attribution,
        )


async def mark_cancelled(post_id: int) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE posts SET status = 'cancelled', scheduled_at = NULL WHERE id = $1",
            post_id,
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
