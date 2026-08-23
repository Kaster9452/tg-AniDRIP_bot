from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class PostCB(CallbackData, prefix="p"):
    """Данные, зашитые в кнопки под предложкой."""

    action: str  # publish | later | go | back | cancel
    post_id: int
    mode: str = "-"  # anon | signed | -


def main_keyboard(post_id: int) -> InlineKeyboardMarkup:
    """Две основные кнопки под каждым предложенным постом."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="📢 Опубликовать",
        callback_data=PostCB(action="publish", post_id=post_id).pack(),
    )
    builder.button(
        text="🕓 Отложить",
        callback_data=PostCB(action="later", post_id=post_id).pack(),
    )
    builder.adjust(2)
    return builder.as_markup()


def mode_keyboard(post_id: int, action: str) -> InlineKeyboardMarkup:
    """Второй шаг: выбрать, показывать ли автора."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🙈 Анонимно",
        callback_data=PostCB(action="go", post_id=post_id, mode=f"anon:{action}").pack(),
    )
    builder.button(
        text="✍️ С подписью",
        callback_data=PostCB(action="go", post_id=post_id, mode=f"signed:{action}").pack(),
    )
    builder.button(
        text="← Назад",
        callback_data=PostCB(action="back", post_id=post_id).pack(),
    )
    builder.adjust(2, 1)
    return builder.as_markup()


def scheduled_keyboard(post_id: int) -> InlineKeyboardMarkup:
    """Под отложенным постом — возможность отменить."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Отменить публикацию",
                    callback_data=PostCB(action="cancel", post_id=post_id).pack(),
                )
            ]
        ]
    )
