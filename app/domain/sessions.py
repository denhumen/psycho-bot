"""Розклад 7 щоденних сесій і старт учасника."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import clock
from app.config import Settings, parse_hhmm
from app.context import AppContext
from app.db.models import Participant, Status, StudySession
from app.domain.allocation import assign_slot
from app.domain.events import BOT, log_event, set_status

DAYS = 7


def kyiv_today(settings: Settings) -> date:
    return clock.now().astimezone(settings.tz).date()


def session_times(start_date: date, reminder_time: str, tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    """(scheduled_for, deadline) у UTC для днів 1..7. Локальний час — у tz, тож перехід
    на літній/зимовий час враховано: дедлайн = scheduled_for наступного дня (23 або 25 год)."""
    at = parse_hhmm(reminder_time)
    scheduled = [
        datetime.combine(start_date + timedelta(days=day - 1), at, tzinfo=tz).astimezone(UTC)
        for day in range(1, DAYS + 1)
    ]
    deadlines = scheduled[1:] + [scheduled[-1] + timedelta(hours=24)]
    return list(zip(scheduled, deadlines, strict=True))


def dev_session_times(base: datetime, offset_days: int, day_minutes: int) -> list[tuple[datetime, datetime]]:
    """DEV_DAY_MINUTES: «день» триває day_minutes хвилин, відлік від моменту старту."""
    day = timedelta(minutes=day_minutes)
    scheduled = [base + (offset_days + d) * day for d in range(DAYS)]
    deadlines = scheduled[1:] + [scheduled[-1] + day]
    return list(zip(scheduled, deadlines, strict=True))


async def reschedule_unsent(session: AsyncSession, participant: Participant, settings: Settings) -> int:
    """/time: перерахувати scheduled_for/deadline лише для ще не надісланих сесій.
    Дедлайн уже надісланої сесії не змінюється (див. DECISIONS.md)."""
    if settings.dev_day_minutes or participant.start_date is None or participant.reminder_time is None:
        return 0
    times = session_times(participant.start_date, participant.reminder_time, settings.tz)
    pending = (
        await session.execute(
            select(StudySession).where(
                StudySession.participant_id == participant.id,
                StudySession.sent_at.is_(None),
                StudySession.missed_at.is_(None),
            )
        )
    ).scalars().all()
    for study_session in pending:
        study_session.scheduled_for, study_session.deadline = times[study_session.day - 1]
    return len(pending)


@dataclass
class StartResult:
    first_instruction_at: datetime
    started_now: bool


async def _first_session(session: AsyncSession, participant_id: int) -> StudySession | None:
    return (
        await session.execute(
            select(StudySession).where(StudySession.participant_id == participant_id, StudySession.day == 1)
        )
    ).scalar_one_or_none()


async def start_participant(ctx: AppContext, participant_id: int) -> StartResult | None:
    """«Готовий почати»: слот + статус active + start_date + 7 сесій — однією транзакцією.
    Повторний виклик повертає той самий результат (started_now=False).
    None — учасник не в стані, з якого можна стартувати."""
    settings = ctx.settings
    async with ctx.allocation_lock:
        async with ctx.db() as session, session.begin():
            participant = await session.get(Participant, participant_id)
            if participant is None:
                return None
            if participant.status != Status.CONSENTED or participant.reminder_time is None:
                first = await _first_session(session, participant_id)
                if first is None:
                    return None
                return StartResult(first.scheduled_for, started_now=False)

            await assign_slot(session, participant.id, settings)
            now = clock.now()
            participant.randomized_at = now
            participant.start_date = kyiv_today(settings) + timedelta(days=settings.start_offset_days)
            set_status(session, participant, Status.ACTIVE, actor=BOT)
            if settings.dev_day_minutes:
                times = dev_session_times(now, settings.start_offset_days, settings.dev_day_minutes)
            else:
                times = session_times(participant.start_date, participant.reminder_time, settings.tz)
            for day, (scheduled_for, deadline) in enumerate(times, start=1):
                session.add(
                    StudySession(
                        participant_id=participant.id, day=day, scheduled_for=scheduled_for, deadline=deadline
                    )
                )
            log_event(session, "randomized", actor=BOT, participant_id=participant.id)
            return StartResult(times[0][0], started_now=True)
