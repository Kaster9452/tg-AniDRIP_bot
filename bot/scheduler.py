"""Фоновая задача: раз в полминуты публикует посты, чей срок подошёл."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot

from bot import database as db
from bot.publisher import PublishError, publish_post

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 30


async def run_scheduler(
    bot: Bot, channel_id: int, admin_group_id: int, tz: ZoneInfo
) -> None:
    logger.info("Планировщик отложенных постов запущен")

    while True:
        try:
            due = await db.get_due_posts(datetime.now(timezone.utc))
            for post in due:
                post_id = post["id"]
                try:
                    await publish_post(
                        bot, post, channel_id, post["with_attribution"]
                    )
                except PublishError as exc:
                    await db.mark_failed(post_id)
                    await bot.send_message(
                        admin_group_id,
                        f"⚠️ Не удалось опубликовать отложенный пост #{post_id}: {exc}",
                    )
                    continue

                signature = "с подписью" if post["with_attribution"] else "анонимно"
                await bot.send_message(
                    admin_group_id,
                    f"✅ Отложенный пост #{post_id} опубликован по расписанию ({signature}).",
                )

        except asyncio.CancelledError:
            logger.info("Планировщик остановлен")
            raise
        except Exception:
            # Планировщик не должен падать насовсем из-за разовой ошибки
            # (например, кратковременной недоступности базы).
            logger.exception("Ошибка в цикле планировщика")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
