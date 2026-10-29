"""Повідомлення «де я і що далі» — для /start від зареєстрованого і /status.
Усе обчислюється з БД, тож після рестарту учасник завжди може продовжити."""

from datetime import datetime

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select

from app.bot import keyboards
from app.clock import clock
from app.context import AppContext
from app.db.models import Participant, Status, StudySession
from app.domain.participants import post_survey_url


def local_date_time(ctx: AppContext, moment: datetime) -> tuple[str, str]:
    local = moment.astimezone(ctx.settings.tz)
    return local.strftime("%d.%m.%Y"), local.strftime("%H:%M")


async def build_status(ctx: AppContext, participant_id: int) -> tuple[str, str, InlineKeyboardMarkup | None]:
    """(текст, text_key, клавіатура) для поточного стану учасника."""
    content = ctx.content
    async with ctx.read() as session:
        participant = await session.get(Participant, participant_id)
        assert participant is not None
        sessions = (
            await session.execute(
                select(StudySession).where(StudySession.participant_id == participant_id).order_by(StudySession.day)
            )
        ).scalars().all()

    status = participant.status
    if status == Status.REGISTERED:
        return content.t("consent"), "consent", keyboards.consent(content)
    if status == Status.CONSENTED and participant.reminder_time is None:
        return content.t("choose_time"), "choose_time", keyboards.reminder_times(ctx.settings.reminder_times)
    if status == Status.CONSENTED:
        text = content.t("time_explanation", time=participant.reminder_time)
        return text, "time_explanation", keyboards.ready(content)
    if status == Status.ACTIVE:
        sent = [s for s in sessions if s.sent_at is not None]
        if not sent:
            date, time = local_date_time(ctx, sessions[0].scheduled_for)
            return content.t("status_waiting", date=date, time=time), "status_waiting", None
        latest = sent[-1]
        markup = None
        if latest.completed_at is not None:
            today = content.t("status_today_done")
        elif latest.missed_at is not None or latest.deadline <= clock.now():
            today = content.t("status_today_missed")
        else:
            today = content.t("status_today_pending")
            markup = keyboards.checkin_start(content, latest.id)
        text = content.t("status_active", day=latest.day, today=today, time=participant.reminder_time)
        return text, "status_active", markup
    if status == Status.COMPLETED:
        url = post_survey_url(ctx.settings, participant.code)
        return content.t("status_completed", url=url), "status_completed", None
    if status == Status.DECLINED:
        return content.t("status_declined"), "status_declined", None
    return content.t("status_withdrawn"), "status_withdrawn", None


async def send_status(ctx: AppContext, participant_id: int, chat_id: int) -> None:
    text, key, markup = await build_status(ctx, participant_id)
    await ctx.sender.send(chat_id, text, participant_id=participant_id, text_key=key, reply_markup=markup)
