"""Агрегати для дашборду і дайджесту. Без розбиття за групами (див. group_counts)."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import AllocationSlot, Message, Participant, Status, StudySession


def local_day_bounds(settings: Settings, day: date) -> tuple[datetime, datetime]:
    """[початок, кінець) календарного дня за Києвом, в UTC."""
    start = datetime.combine(day, time(0, 0), tzinfo=settings.tz)
    end = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=settings.tz)
    return start.astimezone(UTC), end.astimezone(UTC)


async def status_counts(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(select(Participant.status, func.count()).group_by(Participant.status))
    counts = dict.fromkeys(Status.ALL, 0)
    counts.update({status: n for status, n in rows.tuples()})
    return counts


@dataclass
class DayCounts:
    sent: int
    completed: int
    missed: int


async def day_counts(session: AsyncSession, start: datetime, end: datetime) -> DayCounts:
    async def count(column: object) -> int:
        stmt = select(func.count()).select_from(StudySession).where(column >= start, column < end)  # type: ignore[operator]
        return (await session.execute(stmt)).scalar_one()

    return DayCounts(
        sent=await count(StudySession.sent_at),
        completed=await count(StudySession.completed_at),
        missed=await count(StudySession.missed_at),
    )


async def awaiting_checkin(session: AsyncSession, now: datetime) -> int:
    """Надіслані, ще відкриті сесії активних учасників."""
    stmt = (
        select(func.count())
        .select_from(StudySession)
        .join(Participant, Participant.id == StudySession.participant_id)
        .where(
            Participant.status == Status.ACTIVE,
            StudySession.sent_at.is_not(None),
            StudySession.completed_at.is_(None),
            StudySession.missed_at.is_(None),
            StudySession.deadline > now,
        )
    )
    return (await session.execute(stmt)).scalar_one()


async def unread_count(session: AsyncSession) -> int:
    stmt = select(func.count()).select_from(Message).where(Message.direction == "in", Message.read_by_admin_at.is_(None))
    return (await session.execute(stmt)).scalar_one()


async def group_counts(session: AsyncSession) -> dict[str, dict[str, int]]:
    """Лише для owner (інваріант 1): рандомізовано і завершили за групами."""
    randomized = await session.execute(
        select(AllocationSlot.group, func.count())
        .where(AllocationSlot.participant_id.is_not(None))
        .group_by(AllocationSlot.group)
    )
    completed = await session.execute(
        select(AllocationSlot.group, func.count())
        .join(Participant, Participant.id == AllocationSlot.participant_id)
        .where(Participant.status == Status.COMPLETED)
        .group_by(AllocationSlot.group)
    )
    result = {"randomized": {"A": 0, "B": 0}, "completed": {"A": 0, "B": 0}}
    result["randomized"].update(dict(randomized.tuples()))
    result["completed"].update(dict(completed.tuples()))
    return result
