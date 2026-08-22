from __future__ import annotations

import aiosqlite

DB_PATH = "bot.db"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS message_map (
    admin_message_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    user_message_id INTEGER
)
"""


async def init_db(db_path: str = DB_PATH) -> None:
    """Создаёт таблицу, если её ещё нет. Вызывается один раз при старте бота."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(CREATE_TABLE_SQL)
        await db.commit()


async def save_mapping(
    admin_message_id: int,
    user_id: int,
    user_message_id: int | None = None,
    db_path: str = DB_PATH,
) -> None:
    """Запоминает, что сообщение admin_message_id в группе связано с user_id."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO message_map "
            "(admin_message_id, user_id, user_message_id) VALUES (?, ?, ?)",
            (admin_message_id, user_id, user_message_id),
        )
        await db.commit()


async def get_user_id(admin_message_id: int, db_path: str = DB_PATH) -> int | None:
    """По ID сообщения в группе админов возвращает ID пользователя, или None."""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT user_id FROM message_map WHERE admin_message_id = ?",
            (admin_message_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None
