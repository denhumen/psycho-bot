from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot import keyboards
from app.bot.handlers.common import answer
from app.context import AppContext
from app.db.models import Status
from app.domain.events import BOT, set_status
from app.domain.participants import by_chat


async def on_consent(callback: CallbackQuery, ctx: AppContext) -> None:
    chat_id = callback.from_user.id
    agreed = callback.data == "consent:yes"
    participant_id = None
    async with ctx.db() as session, session.begin():
        participant = await by_chat(session, chat_id)
        # Діє лише в стані registered; повторна доставка апдейту нічого не змінює.
        if participant is not None and participant.status == Status.REGISTERED:
            set_status(session, participant, Status.CONSENTED if agreed else Status.DECLINED, actor=BOT)
            participant_id = participant.id
    if participant_id is None:
        await answer(ctx, callback, "button_outdated")
        return

    content = ctx.content
    await answer(ctx, callback)
    await ctx.sender.mark_choice(chat_id, callback.message, content.t("btn_consent_yes" if agreed else "btn_consent_no"))
    if agreed:
        await ctx.sender.send(
            chat_id,
            content.t("choose_time"),
            participant_id=participant_id,
            text_key="choose_time",
            reply_markup=keyboards.reminder_times(ctx.settings.reminder_times),
        )
    else:
        await ctx.sender.send(
            chat_id, content.t("consent_declined"), participant_id=participant_id, text_key="consent_declined"
        )


def make_router() -> Router:
    router = Router(name="consent")
    router.callback_query.register(on_consent, F.data.in_({"consent:yes", "consent:no"}))
    return router
