from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    MetaData,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.clock import clock


class UTCDateTime(TypeDecorator[datetime]):
    """Зберігає час у SQLite як наївний UTC, повертає aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("у БД пишемо лише datetime з tz")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


def _now() -> datetime:
    return clock.now()


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )
    type_annotation_map = {datetime: UTCDateTime, dict[str, Any]: JSON}


class Created:
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Status:
    REGISTERED = "registered"
    CONSENTED = "consented"
    ACTIVE = "active"
    COMPLETED = "completed"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"

    ALL = (REGISTERED, CONSENTED, ACTIVE, COMPLETED, DECLINED, WITHDRAWN)


class Kind:
    INSTRUCTION = "instruction"
    CHECKIN = "checkin"
    AUTO_REMINDER = "auto_reminder"
    MANUAL = "manual"
    SYSTEM = "system"
    PARTICIPANT_TEXT = "participant_text"


class Participant(Created, Base):
    __tablename__ = "participants"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default=Status.REGISTERED)
    reminder_time: Mapped[str | None] = mapped_column(String(5))
    start_date: Mapped[date | None]
    consented_at: Mapped[datetime | None]
    randomized_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    withdrawn_at: Mapped[datetime | None]
    post_survey_sent_at: Mapped[datetime | None]
    last_activity_at: Mapped[datetime | None]
    bot_blocked: Mapped[bool] = mapped_column(default=False)


class Contact(Created, Base):
    __tablename__ = "contacts"

    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)


class AllowedCode(Created, Base):
    __tablename__ = "allowed_codes"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    uploaded_at: Mapped[datetime] = mapped_column(default=_now)
    uploaded_by: Mapped[str | None] = mapped_column(String(64))


class AllocationSlot(Created, Base):
    __tablename__ = "allocation_slots"

    slot_no: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    block_no: Mapped[int]
    group: Mapped[str] = mapped_column(String(1))
    participant_id: Mapped[int | None] = mapped_column(ForeignKey("participants.id"), unique=True)
    assigned_at: Mapped[datetime | None]


class StudySession(Created, Base):
    __tablename__ = "sessions"
    __table_args__ = (UniqueConstraint("participant_id", "day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), index=True)
    day: Mapped[int]
    scheduled_for: Mapped[datetime] = mapped_column(index=True)
    deadline: Mapped[datetime]
    sent_at: Mapped[datetime | None]
    reminder_sent_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    missed_at: Mapped[datetime | None]


class CheckinAnswer(Created, Base):
    __tablename__ = "checkin_answers"
    __table_args__ = (UniqueConstraint("session_id", "question_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), index=True)
    question_id: Mapped[str] = mapped_column(String(64))
    value: Mapped[str] = mapped_column(String(64))
    answered_at: Mapped[datetime] = mapped_column(default=_now)


class Message(Created, Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("participant_id", "direction", "telegram_message_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), index=True)
    direction: Mapped[str] = mapped_column(String(3))
    kind: Mapped[str] = mapped_column(String(20))
    sender_admin_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id"))
    text: Mapped[str] = mapped_column(Text)
    text_key: Mapped[str | None] = mapped_column(String(64))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    session_day: Mapped[int | None]
    read_by_admin_at: Mapped[datetime | None]


class ReminderTemplate(Created, Base):
    __tablename__ = "reminder_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True)


class AdminUser(Created, Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))
    is_active: Mapped[bool] = mapped_column(default=True)
    last_login_at: Mapped[datetime | None]


class Event(Created, Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(default=_now, index=True)
    actor: Mapped[str] = mapped_column(String(32))
    participant_id: Mapped[int | None] = mapped_column(ForeignKey("participants.id"), index=True)
    type: Mapped[str] = mapped_column(String(40), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)


class ProcessedUpdate(Created, Base):
    """update_id уже оброблених апдейтів Telegram: після рестарту getUpdates може
    повернути їх знову (offset підтверджується лише наступним запитом)."""

    __tablename__ = "processed_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)


class Setting(Created, Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
