"""Фоновая задача: раз в полминуты публикует посты, чей срок подошёл."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot

from bot import database as db
from bot.publisher import PublishError, publish_post

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 30
SUBSCRIBER_SAMPLE_INTERVAL_SECONDS = 10 * 60
CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60
MESSAGE_MAP_RETENTION = timedelta(days=3)


async def run_scheduler(
    bot: Bot, channel_id: int, admin_group_id: int, tz: ZoneInfo
) -> None:
    logger.info("Планировщик отложенных постов запущен")
    last_subscriber_sample = datetime.min.replace(tzinfo=timezone.utc)
    last_cleanup = datetime.min.replace(tzinfo=timezone.utc)

    while True:
        try:
            now = datetime.now(timezone.utc)
            if (
                now - last_subscriber_sample
            ).total_seconds() >= SUBSCRIBER_SAMPLE_INTERVAL_SECONDS:
                try:
                    subscriber_count = await bot.get_chat_member_count(channel_id)
                    await db.record_subscriber_count(subscriber_count, now)
                except Exception:
                    logger.exception("Не удалось получить число подписчиков канала")
                finally:
                    last_subscriber_sample = now

            if (now - last_cleanup).total_seconds() >= CLEANUP_INTERVAL_SECONDS:
                try:
                    removed = await db.purge_old_message_map(now - MESSAGE_MAP_RETENTION)
                    if removed:
                        logger.info("Очищено %s старых записей message_map", removed)
                except Exception:
                    logger.exception("Не удалось очистить message_map")
                finally:
                    last_cleanup = now

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
