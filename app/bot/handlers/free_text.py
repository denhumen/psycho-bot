from aiogram import Router
from aiogram.types import Message

from app.bot.handlers.registration import handle_code
from app.context import AppContext
from app.domain.participants import by_chat


async def on_any_message(message: Message, ctx: AppContext) -> None:
    chat_id = message.chat.id
    async with ctx.read() as session:
        participant = await by_chat(session, chat_id)
    if participant is None:
        # Будь-який текст від незареєстрованого chat_id — це спроба ввести код.
        if message.text and not message.text.startswith("/"):
            await handle_code(ctx, chat_id, message.text)
        else:
            await ctx.sender.send(chat_id, ctx.content.t("not_registered"))
        return


def make_router() -> Router:
    router = Router(name="free_text")
    router.message.register(on_any_message)
    return router
