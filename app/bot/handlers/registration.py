from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot import keyboards
from app.bot.handlers.common import answer
from app.bot.views import local_date_time, send_status
from app.context import AppContext
from app.domain.participants import Registration, by_chat, register_code
from app.domain.sessions import start_participant


_REPLY_KEYS = {
    Registration.INVALID_FORMAT: "code_invalid_format",
    Registration.NOT_FOUND: "code_not_found",
    Registration.CONFLICT: "code_conflict",
}


async def handle_code(ctx: AppContext, chat_id: int, raw: str) -> None:
    result = await register_code(ctx, chat_id, raw)
    if result.outcome in _REPLY_KEYS:
        # Учасника для цього chat_id ще немає — у messages не логуємо.
        key = _REPLY_KEYS[result.outcome]
        await ctx.sender.send(chat_id, ctx.content.t(key), text_key=key)
        return
    assert result.participant_id is not None
    if result.outcome == Registration.ALREADY_REGISTERED:
        await send_status(ctx, result.participant_id, chat_id)
        return
    await ctx.sender.send(
        chat_id,
        ctx.content.t("consent"),
        participant_id=result.participant_id,
        text_key="consent",
        reply_markup=keyboards.consent(ctx.content),
    )


async def on_ready(callback: CallbackQuery, ctx: AppContext) -> None:
    chat_id = callback.from_user.id
    async with ctx.read() as session:
        participant = await by_chat(session, chat_id)
    result = await start_participant(ctx, participant.id) if participant is not None else None
    if participant is None or result is None:
        await answer(ctx, callback, "button_outdated")
        return
    await answer(ctx, callback)
    await ctx.sender.mark_choice(chat_id, callback.message, ctx.content.t("btn_ready"))
    if result.started_now:
        date, time = local_date_time(ctx, result.first_instruction_at)
        await ctx.sender.send(
            chat_id,
            ctx.content.t("started", date=date, time=time),
            participant_id=participant.id,
            text_key="started",
        )


def make_router() -> Router:
    router = Router(name="registration")
    router.callback_query.register(on_ready, F.data == "ready")
    return router
