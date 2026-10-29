from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards
from app.bot.handlers.common import answer
from app.bot.views import send_status
from app.context import AppContext
from app.db.models import Participant, Status
from app.domain.events import BOT, set_status
from app.domain.participants import by_chat

STOPPABLE = (Status.REGISTERED, Status.CONSENTED, Status.ACTIVE)


async def _participant(ctx: AppContext, chat_id: int) -> Participant | None:
    async with ctx.read() as session:
        participant = await by_chat(session, chat_id)
    if participant is None:
        await ctx.sender.send(chat_id, ctx.content.t("not_registered"))
    return participant


async def on_status(message: Message, ctx: AppContext) -> None:
    participant = await _participant(ctx, message.chat.id)
    if participant is not None:
        await send_status(ctx, participant.id, message.chat.id)


async def on_help(message: Message, ctx: AppContext) -> None:
    async with ctx.read() as session:
        participant = await by_chat(session, message.chat.id)
    await ctx.sender.send(
        message.chat.id,
        ctx.content.t("help"),
        participant_id=participant.id if participant else None,
        text_key="help",
    )


async def on_time_command(message: Message, ctx: AppContext) -> None:
    chat_id = message.chat.id
    participant = await _participant(ctx, chat_id)
    if participant is None:
        return
    if participant.status in (Status.CONSENTED, Status.ACTIVE):
        await ctx.sender.send(
            chat_id,
            ctx.content.t("time_choose_new"),
            participant_id=participant.id,
            text_key="time_choose_new",
            reply_markup=keyboards.reminder_times(ctx.settings.reminder_times),
        )
    else:
        await ctx.sender.send(
            chat_id, ctx.content.t("time_unavailable"), participant_id=participant.id, text_key="time_unavailable"
        )


async def on_stop_command(message: Message, ctx: AppContext) -> None:
    chat_id = message.chat.id
    participant = await _participant(ctx, chat_id)
    if participant is None:
        return
    if participant.status in STOPPABLE:
        await ctx.sender.send(
            chat_id,
            ctx.content.t("stop_confirm"),
            participant_id=participant.id,
            text_key="stop_confirm",
            reply_markup=keyboards.stop(ctx.content),
        )
    else:
        await ctx.sender.send(
            chat_id, ctx.content.t("stop_unavailable"), participant_id=participant.id, text_key="stop_unavailable"
        )


async def on_stop_answer(callback: CallbackQuery, ctx: AppContext) -> None:
    chat_id = callback.from_user.id
    confirmed = callback.data == "stop:yes"
    participant_id = None
    async with ctx.db() as session, session.begin():
        participant = await by_chat(session, chat_id)
        if participant is not None and participant.status in STOPPABLE:
            participant_id = participant.id
            if confirmed:
                # Дані не видаляються: лише статус + подія.
                set_status(session, participant, Status.WITHDRAWN, actor=BOT)
    if participant_id is None:
        await answer(ctx, callback, "button_outdated")
        return
    await answer(ctx, callback)
    content = ctx.content
    await ctx.sender.mark_choice(chat_id, callback.message, content.t("btn_stop_yes" if confirmed else "btn_stop_no"))
    key = "stopped" if confirmed else "stop_cancelled"
    await ctx.sender.send(chat_id, content.t(key), participant_id=participant_id, text_key=key)


def make_router() -> Router:
    router = Router(name="commands")
    router.message.register(on_status, Command("status"))
    router.message.register(on_help, Command("help"))
    router.message.register(on_time_command, Command("time"))
    router.message.register(on_stop_command, Command("stop"))
    router.callback_query.register(on_stop_answer, F.data.in_({"stop:yes", "stop:no"}))
    return router
