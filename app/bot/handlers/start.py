from aiogram import Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message

from app.bot.handlers.registration import handle_code
from app.bot.views import send_status
from app.context import AppContext
from app.domain.participants import by_chat


async def on_start(message: Message, command: CommandObject, ctx: AppContext) -> None:
    chat_id = message.chat.id
    async with ctx.read() as session:
        participant = await by_chat(session, chat_id)
    if participant is not None:
        await send_status(ctx, participant.id, chat_id)
    elif command.args:
        await handle_code(ctx, chat_id, command.args)
    else:
        await ctx.sender.send(chat_id, ctx.content.t("welcome_ask_code"))


def make_router() -> Router:
    router = Router(name="start")
    router.message.register(on_start, CommandStart())
    return router
