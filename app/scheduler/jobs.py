"""Задачі планувальника. Кожна ідемпотентна і спирається тільки на БД:
повторний тік або рестарт не дублюють дій."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards
from app.bot.sender import AfterSent
from app.clock import clock
from app.config import parse_hhmm
from app.context import AppContext
from app.db.models import AllocationSlot, Contact, Kind, Participant, Status, StudySession
from app.domain.backup import send_backup
from app.domain.events import SYSTEM, log_event, set_status
from app.domain.participants import post_survey_url
from app.domain.state import LAST_BACKUP_AT, LAST_DIGEST_AT, get_time, is_paused, set_time
from app.domain.stats import day_counts, local_day_bounds, status_counts, unread_count

log = logging.getLogger(__name__)

MASS_SEND_PAUSE_SECONDS = 0.05
# Після невдалої спроби (не Forbidden) повторюємо не раніше ніж через стільки.
SEND_RETRY_DELAY = timedelta(minutes=10)


def last_due_moment(ctx: AppContext, hhmm: str) -> datetime:
    """Останній момент HH:MM за Києвом, що вже настав (сьогодні або вчора), в UTC."""
    tz = ctx.settings.tz
    now_local = clock.now().astimezone(tz)
    at = parse_hhmm(hhmm)
    due = datetime.combine(now_local.date(), at, tzinfo=tz)
    if due > now_local:
        due = datetime.combine(now_local.date() - timedelta(days=1), at, tzinfo=tz)
    return due.astimezone(UTC)


def next_moment(ctx: AppContext, hhmm: str) -> datetime:
    """Найближчий майбутній момент HH:MM за Києвом, в UTC (для «наступного бекапу»)."""
    tz = ctx.settings.tz
    now_local = clock.now().astimezone(tz)
    at = parse_hhmm(hhmm)
    due = datetime.combine(now_local.date(), at, tzinfo=tz)
    if due <= now_local:
        due = datetime.combine(now_local.date() + timedelta(days=1), at, tzinfo=tz)
    return due.astimezone(UTC)


def _stamp_session(column: Any, session_id: int) -> AfterSent:
    async def after(session: AsyncSession) -> None:
        await session.execute(
            update(StudySession)
            .where(StudySession.id == session_id, column.is_(None))
            .values({column: clock.now()})
        )

    return after


class Jobs:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.pause_seconds = MASS_SEND_PAUSE_SECONDS
        # Лише темп повторів після помилок; стан учасників — у БД.
        self._retry_at: dict[tuple[str, int], datetime] = {}

    def _may_try(self, key: tuple[str, int]) -> bool:
        retry_at = self._retry_at.get(key)
        return retry_at is None or clock.now() >= retry_at

    def _record(self, key: tuple[str, int], ok: bool, blocked: bool) -> None:
        if ok or blocked:
            self._retry_at.pop(key, None)
        else:
            self._retry_at[key] = clock.now() + SEND_RETRY_DELAY

    async def _paused(self) -> bool:
        # Перевіряємо перед кожним надсиланням: пауза діє навіть посеред розсилки.
        async with self.ctx.read() as session:
            return await is_paused(session)

    def _open_sessions(self) -> Select[tuple[StudySession, int, str]]:
        return (
            select(StudySession, Contact.chat_id, AllocationSlot.group)
            .join(Participant, Participant.id == StudySession.participant_id)
            .join(Contact, Contact.participant_id == Participant.id)
            .join(AllocationSlot, AllocationSlot.participant_id == Participant.id)
            .where(
                Participant.status == Status.ACTIVE,
                Participant.bot_blocked.is_(False),
                StudySession.completed_at.is_(None),
                StudySession.missed_at.is_(None),
                StudySession.deadline > clock.now(),
            )
            .order_by(StudySession.scheduled_for)
        )

    # --- 1. закриття прострочених -------------------------------------------

    async def close_expired(self) -> None:
        """deadline минув, сесію не завершено → missed. Сюди ж потрапляють сесії, які
        не встигли надіслати через простій: їх не надсилаємо, а позначаємо пропущеними."""
        now = clock.now()
        async with self.ctx.db() as session, session.begin():
            expired = (
                await session.execute(
                    select(StudySession)
                    .join(Participant, Participant.id == StudySession.participant_id)
                    .where(
                        Participant.status == Status.ACTIVE,
                        StudySession.deadline <= now,
                        StudySession.completed_at.is_(None),
                        StudySession.missed_at.is_(None),
                    )
                )
            ).scalars().all()
            for study_session in expired:
                study_session.missed_at = now
                log_event(
                    session,
                    "session_missed",
                    participant_id=study_session.participant_id,
                    day=study_session.day,
                    sent=study_session.sent_at is not None,
                )

    # --- 2. розсилка інструкцій ---------------------------------------------

    async def send_due(self) -> None:
        async with self.ctx.read() as session:
            rows = (
                await session.execute(
                    self._open_sessions().where(
                        StudySession.scheduled_for <= clock.now(), StudySession.sent_at.is_(None)
                    )
                )
            ).all()
        content = self.ctx.content
        for study_session, chat_id, group in rows:
            key = ("instruction", study_session.id)
            if not self._may_try(key) or await self._paused():
                continue
            result = await self.ctx.sender.send(
                chat_id,
                content.instructions.text(group, study_session.day),
                participant_id=study_session.participant_id,
                kind=Kind.INSTRUCTION,
                session_day=study_session.day,
                reply_markup=keyboards.checkin_start(content, study_session.id),
                after=_stamp_session(StudySession.sent_at, study_session.id),
            )
            self._record(key, result.ok, result.blocked)
            await asyncio.sleep(self.pause_seconds)

    # --- 3. авто-нагадування ------------------------------------------------

    async def auto_reminders(self) -> None:
        hours = self.ctx.settings.auto_reminder_after_hours
        if hours <= 0:
            return
        async with self.ctx.read() as session:
            rows = (
                await session.execute(
                    self._open_sessions().where(
                        StudySession.sent_at <= clock.now() - timedelta(hours=hours),
                        StudySession.reminder_sent_at.is_(None),
                    )
                )
            ).all()
        content = self.ctx.content
        for study_session, chat_id, _group in rows:
            key = ("reminder", study_session.id)
            if not self._may_try(key) or await self._paused():
                continue
            # Інваріант 6: один нейтральний текст для всіх.
            result = await self.ctx.sender.send(
                chat_id,
                content.t("auto_reminder"),
                participant_id=study_session.participant_id,
                kind=Kind.AUTO_REMINDER,
                text_key="auto_reminder",
                session_day=study_session.day,
                reply_markup=keyboards.checkin_start(content, study_session.id),
                after=_stamp_session(StudySession.reminder_sent_at, study_session.id),
            )
            self._record(key, result.ok, result.blocked)
            await asyncio.sleep(self.pause_seconds)

    # --- 4. завершення участі -----------------------------------------------

    async def finish_participants(self) -> None:
        # Фаза 1: день 7 закрито → completed.
        async with self.ctx.db() as session, session.begin():
            finished = (
                await session.execute(
                    select(Participant)
                    .join(StudySession, StudySession.participant_id == Participant.id)
                    .where(
                        Participant.status == Status.ACTIVE,
                        StudySession.day == 7,
                        (StudySession.completed_at.is_not(None)) | (StudySession.missed_at.is_not(None)),
                    )
                )
            ).scalars().all()
            for participant in finished:
                set_status(session, participant, Status.COMPLETED, actor=SYSTEM)

        # Фаза 2: фінальне посилання — окремо, щоб збій надсилання не губив його.
        async with self.ctx.read() as session:
            pending = (
                await session.execute(
                    select(Participant, Contact.chat_id)
                    .join(Contact, Contact.participant_id == Participant.id)
                    .where(
                        Participant.status == Status.COMPLETED,
                        Participant.post_survey_sent_at.is_(None),
                        Participant.bot_blocked.is_(False),
                    )
                )
            ).all()
        for participant, chat_id in pending:
            key = ("final", participant.id)
            if not self._may_try(key) or await self._paused():
                continue
            url = post_survey_url(self.ctx.settings, participant.code)
            result = await self.ctx.sender.send(
                chat_id,
                self.ctx.content.t("final_message", url=url),
                participant_id=participant.id,
                text_key="final_message",
                after=self._mark_post_survey(participant.id),
            )
            self._record(key, result.ok, result.blocked)
            await asyncio.sleep(self.pause_seconds)

    @staticmethod
    def _mark_post_survey(participant_id: int) -> AfterSent:
        async def after(session: AsyncSession) -> None:
            await session.execute(
                update(Participant)
                .where(Participant.id == participant_id, Participant.post_survey_sent_at.is_(None))
                .values(post_survey_sent_at=clock.now())
            )
            log_event(session, "post_survey_sent", participant_id=participant_id)

        return after

    # --- 5. нічний бекап ----------------------------------------------------

    async def nightly_backup(self) -> None:
        chat_id = self.ctx.settings.backup_chat_id
        if chat_id is None:
            return
        due = last_due_moment(self.ctx, self.ctx.settings.backup_time)
        async with self.ctx.read() as session:
            last = await get_time(session, LAST_BACKUP_AT)
        if last is not None and last >= due:
            return
        await send_backup(self.ctx, chat_id)
        async with self.ctx.db() as session, session.begin():
            await set_time(session, LAST_BACKUP_AT, clock.now())
            log_event(session, "backup_sent", trigger="nightly")

    # --- 6. щоденний дайджест -----------------------------------------------

    async def daily_digest(self) -> None:
        chat_id = self.ctx.settings.admin_chat_id
        if chat_id is None:
            return
        due = last_due_moment(self.ctx, self.ctx.settings.digest_time)
        async with self.ctx.read() as session:
            last = await get_time(session, LAST_DIGEST_AT)
            if last is not None and last >= due:
                return
            text = await self.digest_text(session)
        result = await self.ctx.sender.send(chat_id, text)
        if not result.ok:
            raise RuntimeError("дайджест не надіслано")
        async with self.ctx.db() as session, session.begin():
            await set_time(session, LAST_DIGEST_AT, clock.now())
            log_event(session, "digest_sent")

    async def digest_text(self, session: AsyncSession) -> str:
        """Службовий текст для команди (не для учасників); лише агрегати, без груп."""
        settings = self.ctx.settings
        yesterday = clock.now().astimezone(settings.tz).date() - timedelta(days=1)
        counts = await status_counts(session)
        day = await day_counts(session, *local_day_bounds(settings, yesterday))
        unread = await unread_count(session)
        return (
            f"Дайджест дослідження за {yesterday:%d.%m.%Y}\n\n"
            f"Зареєстровано: {sum(counts.values())}\n"
            f"Активні: {counts[Status.ACTIVE]}\n"
            f"Завершили: {counts[Status.COMPLETED]}\n"
            f"Вийшли: {counts[Status.WITHDRAWN]}\n"
            f"Не погодились: {counts[Status.DECLINED]}\n\n"
            f"Вчора: інструкцій надіслано {day.sent}, чек-інів пройдено {day.completed}, пропущено {day.missed}\n"
            f"Непрочитаних повідомлень: {unread}"
        )
