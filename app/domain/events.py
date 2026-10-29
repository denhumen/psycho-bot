from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import clock
from app.db.models import Event, Participant, Status

SYSTEM = "system"
BOT = "bot"


def admin_actor(admin_id: int) -> str:
    return f"admin:{admin_id}"


def log_event(
    session: AsyncSession,
    type_: str,
    *,
    actor: str = SYSTEM,
    participant_id: int | None = None,
    **payload: Any,
) -> None:
    # Інваріант 1: група ніколи не потрапляє в аудит.
    assert "group" not in payload, "група учасника не пишеться в events"
    session.add(Event(actor=actor, participant_id=participant_id, type=type_, payload=payload))


def set_status(session: AsyncSession, participant: Participant, new_status: str, *, actor: str) -> None:
    assert new_status in Status.ALL
    old_status = participant.status
    if old_status == new_status:
        return
    participant.status = new_status
    now = clock.now()
    if new_status == Status.CONSENTED:
        participant.consented_at = now
    elif new_status == Status.COMPLETED:
        participant.completed_at = now
    elif new_status == Status.WITHDRAWN:
        participant.withdrawn_at = now
    log_event(
        session,
        "status_changed",
        actor=actor,
        participant_id=participant.id,
        old=old_status,
        new=new_status,
    )
