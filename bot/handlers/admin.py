import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message

from bot.database import get_user_id

logger = logging.getLogger(__name__)

router = Router(name="admin")


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_admin_reply(message: Message, bot: Bot, admin_group_id: int) -> None:
    if message.chat.id != admin_group_id:
        return  # сообщение из какой-то другой группы — не наше дело

    if not message.reply_to_message:
        return  # обычное сообщение админов между собой, не ответ юзеру

    target_user_id = await get_user_id(message.reply_to_message.message_id)
    if target_user_id is None:
        return  # реплай не на пересланное ботом сообщение

    try:
        await message.copy_to(chat_id=target_user_id)
    except TelegramForbiddenError:
        await message.reply("⚠️ Пользователь заблокировал бота, ответ не доставлен.")
    except TelegramBadRequest:
        logger.exception("Не удалось отправить ответ пользователю %s", target_user_id)
        await message.reply("⚠️ Не удалось отправить сообщение.")
