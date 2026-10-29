from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot import keyboards
from app.bot.handlers.common import answer
from app.context import AppContext
from app.db.models import Status
from app.domain.events import BOT, log_event
from app.domain.participants import by_chat


async def on_time(callback: CallbackQuery, ctx: AppContext) -> None:
    chat_id = callback.from_user.id
    chosen = (callback.data or "").removeprefix("time:")
    if chosen not in ctx.settings.reminder_times:
        await answer(ctx, callback, "button_outdated")
        return

    participant_id = None
    changed = False
    async with ctx.db() as session, session.begin():
        participant = await by_chat(session, chat_id)
        if participant is not None and participant.status == Status.CONSENTED:
            participant_id = participant.id
            changed = participant.reminder_time != chosen
            if changed:
                participant.reminder_time = chosen
                log_event(session, "reminder_time_set", actor=BOT, participant_id=participant.id, time=chosen)
    if participant_id is None:
        await answer(ctx, callback, "button_outdated")
        return

    await answer(ctx, callback)
    await ctx.sender.mark_choice(chat_id, callback.message, chosen)
    if changed:
        await ctx.sender.send(
            chat_id,
            ctx.content.t("time_explanation", time=chosen),
            participant_id=participant_id,
            text_key="time_explanation",
            reply_markup=keyboards.ready(ctx.content),
        )


def make_router() -> Router:
    router = Router(name="time")
    router.callback_query.register(on_time, F.data.startswith("time:"))
    return router
