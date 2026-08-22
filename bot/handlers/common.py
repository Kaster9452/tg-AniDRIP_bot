from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="common")


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    """Полезно на этапе настройки: узнать ID группы для ADMIN_GROUP_ID."""
    await message.reply(f"Chat ID: <code>{message.chat.id}</code>")
