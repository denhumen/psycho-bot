from aiogram.exceptions import TelegramAPIError
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import CallbackQuery

from app.context import AppContext


async def answer(ctx: AppContext, callback: CallbackQuery, text_key: str | None = None) -> None:
    """answer_callback_query завжди — інакше в Telegram крутиться «годинник» на кнопці."""
    text = ctx.content.t(text_key) if text_key else None
    try:
        await ctx.sender.call(AnswerCallbackQuery(callback_query_id=callback.id, text=text))
    except TelegramAPIError:
        # Запит застарів (наприклад, після простою) — нічого страшного.
        pass
