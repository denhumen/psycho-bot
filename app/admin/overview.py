"""Дані для таблиці учасників і картки. Група не завантажується взагалі (інваріант 1)."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message, Participant, Status, StudySession
from app.domain.sessions import DAYS

DONE, MISSED, WAITING, NOT_SENT = "●", "✕", "○", "·"
CELL_TITLES = {DONE: "пройдено", MISSED: "пропущено", WAITING: "надіслано, очікує", NOT_SENT: "ще не надіслано"}


def cell(study_session: StudySession | None, now: datetime) -> str:
    if study_session is None or study_session.sent_at is None:
        return MISSED if study_session is not None and study_session.missed_at is not None else NOT_SENT
    if study_session.completed_at is not None:
        return DONE
    if study_session.missed_at is not None or study_session.deadline <= now:
        return MISSED
    return WAITING


@dataclass
class Row:
    participant: Participant
    sessions: list[StudySession | None]
    unread: int
    now: datetime
    cells: list[str] = field(init=False)

    def __post_init__(self) -> None:
        self.cells = [cell(s, self.now) for s in self.sessions]

    @property
    def day(self) -> int:
        sent = [s.day for s in self.sessions if s is not None and s.sent_at is not None]
        return max(sent, default=0)

    @property
    def pending_now(self) -> bool:
        """«Не пройшли сьогодні»: остання надіслана сесія ще відкрита і не пройдена."""
        return self.participant.status == Status.ACTIVE and self.day > 0 and self.cells[self.day - 1] == WAITING

    @property
    def missed_streak(self) -> bool:
        """«Пропустили 2+ дні поспіль»: дві останні закриті сесії — пропущені."""
        closed = [c for c in self.cells if c in (DONE, MISSED)]
        return len(closed) >= 2 and closed[-1] == MISSED and closed[-2] == MISSED

    @property
    def not_started(self) -> bool:
        return self.participant.status in (Status.REGISTERED, Status.CONSENTED)


async def unread_by_participant(session: AsyncSession) -> dict[int, int]:
    rows = await session.execute(
        select(Message.participant_id, func.count())
        .where(Message.direction == "in", Message.read_by_admin_at.is_(None))
        .group_by(Message.participant_id)
    )
    return dict(rows.tuples().all())


async def load_rows(session: AsyncSession, now: datetime) -> list[Row]:
    participants = (await session.execute(select(Participant).order_by(Participant.created_at))).scalars().all()
    by_participant: dict[int, list[StudySession | None]] = defaultdict(lambda: [None] * DAYS)
    for study_session in (await session.execute(select(StudySession))).scalars():
        by_participant[study_session.participant_id][study_session.day - 1] = study_session
    unread = await unread_by_participant(session)
    return [Row(p, by_participant[p.id], unread.get(p.id, 0), now) for p in participants]


def apply_filters(rows: list[Row], *, status: str, flag: str, q: str) -> list[Row]:
    if status:
        rows = [r for r in rows if r.participant.status == status]
    if flag == "not_done_today":
        rows = [r for r in rows if r.pending_now]
    elif flag == "missed_streak":
        rows = [r for r in rows if r.missed_streak]
    elif flag == "unread":
        rows = [r for r in rows if r.unread > 0]
    elif flag == "not_started":
        rows = [r for r in rows if r.not_started]
    elif flag == "blocked":
        rows = [r for r in rows if r.participant.bot_blocked]
    if q.strip():
        needle = q.strip().upper()
        rows = [r for r in rows if needle in r.participant.code]
    return rows
