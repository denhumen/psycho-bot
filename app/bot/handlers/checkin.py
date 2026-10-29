"""Чек-ін: питання по одному. Наступне питання завжди — перше без відповіді в БД,
тож після рестарту кнопка «Пройти коротке опитування» продовжує з того самого місця."""

from enum import StrEnum

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards
from app.bot.handlers.common import answer
from app.clock import clock
from app.context import AppContext
from app.db.models import CheckinAnswer, Kind, Participant, Status, StudySession
from app.domain.content import ChoiceQuestion, ScaleQuestion
from app.domain.events import BOT, log_event
from app.domain.participants import by_chat


class Check(StrEnum):
    OK = "ok"
    OUTDATED = "button_outdated"
    EXPIRED = "checkin_expired"
    DONE = "checkin_already_done"


def _check(participant: Participant | None, study_session: StudySession | None) -> Check:
    if participant is None or study_session is None or study_session.participant_id != participant.id:
        return Check.OUTDATED
    if study_session.completed_at is not None:
        return Check.DONE
    if study_session.missed_at is not None or clock.now() >= study_session.deadline:
        return Check.EXPIRED
    if participant.status != Status.ACTIVE or study_session.sent_at is None:
        return Check.OUTDATED
    return Check.OK


async def _answered(session: AsyncSession, session_id: int) -> set[str]:
    rows = await session.execute(select(CheckinAnswer.question_id).where(CheckinAnswer.session_id == session_id))
    return set(rows.scalars())


def _next_question(ctx: AppContext, answered: set[str]) -> ChoiceQuestion | ScaleQuestion | None:
    return next((q for q in ctx.content.checkin.questions if q.id not in answered), None)


async def _send_question(
    ctx: AppContext, chat_id: int, participant_id: int, study_session: StudySession, q: ChoiceQuestion | ScaleQuestion
) -> None:
    await ctx.sender.send(
        chat_id,
        q.text,
        participant_id=participant_id,
        kind=Kind.CHECKIN,
        text_key=f"checkin:{q.id}",
        session_day=study_session.day,
        reply_markup=keyboards.question(q, study_session.id),
    )


async def on_checkin_start(callback: CallbackQuery, ctx: AppContext) -> None:
    """cs:{session_id} — почати або продовжити опитування."""
    chat_id = callback.from_user.id
    session_id = int((callback.data or "").split(":")[1])
    async with ctx.read() as session:
        participant = await by_chat(session, chat_id)
        study_session = await session.get(StudySession, session_id)
        check = _check(participant, study_session)
        answered = await _answered(session, session_id) if check == Check.OK else set()
    if check != Check.OK:
        await answer(ctx, callback, check.value)
        return
    assert participant is not None and study_session is not None
    await answer(ctx, callback)
    q = _next_question(ctx, answered)
    if q is not None:
        await _send_question(ctx, chat_id, participant.id, study_session, q)


async def on_checkin_answer(callback: CallbackQuery, ctx: AppContext) -> None:
    """ci:{session_id}:{question_id}:{value}"""
    chat_id = callback.from_user.id
    _, raw_session_id, question_id, value = (callback.data or "").split(":", 3)
    q = ctx.content.checkin.by_id(question_id)
    labels = dict(q.choices()) if q is not None else {}
    if value not in labels:
        await answer(ctx, callback, Check.OUTDATED.value)
        return

    inserted = False
    finished = False
    next_q = None
    async with ctx.db() as session, session.begin():
        participant = await by_chat(session, chat_id)
        study_session = await session.get(StudySession, int(raw_session_id))
        check = _check(participant, study_session)
        if check == Check.OK:
            assert participant is not None and study_session is not None
            result = await session.execute(
                insert(CheckinAnswer)
                .values(
                    session_id=study_session.id,
                    question_id=question_id,
                    value=value,
                    answered_at=clock.now(),
                    created_at=clock.now(),
                )
                .on_conflict_do_nothing()
            )
            # Перша відповідь фіксується; повторне натискання нічого не змінює.
            inserted = result.rowcount == 1
            next_q = _next_question(ctx, await _answered(session, study_session.id))
            if inserted and next_q is None:
                study_session.completed_at = clock.now()
                finished = True
                log_event(session, "checkin_completed", actor=BOT, participant_id=participant.id, day=study_session.day)
    if check != Check.OK:
        await answer(ctx, callback, check.value)
        return
    assert participant is not None and study_session is not None
    await answer(ctx, callback)
    if not inserted:
        return
    await ctx.sender.mark_choice(chat_id, callback.message, labels[value])
    if finished:
        await ctx.sender.send(
            chat_id, ctx.content.t("checkin_thanks"), participant_id=participant.id, text_key="checkin_thanks"
        )
    elif next_q is not None:
        await _send_question(ctx, chat_id, participant.id, study_session, next_q)


def make_router() -> Router:
    router = Router(name="checkin")
    router.callback_query.register(on_checkin_start, F.data.regexp(r"^cs:\d+$"))
    router.callback_query.register(on_checkin_answer, F.data.regexp(r"^ci:\d+:[a-z0-9_]+:[a-z0-9_]+$"))
    return router
