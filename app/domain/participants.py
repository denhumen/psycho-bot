from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import clock
from app.config import Settings
from app.context import AppContext
from app.db.models import AllowedCode, Contact, Participant
from app.domain.events import BOT, log_event


def normalize_code(raw: str) -> str:
    return raw.strip().upper()


def post_survey_url(settings: Settings, code: str) -> str:
    return settings.post_survey_url_template.replace("{code}", quote(code, safe=""))


async def by_chat(session: AsyncSession, chat_id: int) -> Participant | None:
    return (
        await session.execute(
            select(Participant).join(Contact, Contact.participant_id == Participant.id).where(Contact.chat_id == chat_id)
        )
    ).scalar_one_or_none()


async def chat_of(session: AsyncSession, participant_id: int) -> int | None:
    return (
        await session.execute(select(Contact.chat_id).where(Contact.participant_id == participant_id))
    ).scalar_one_or_none()


class Registration(StrEnum):
    INVALID_FORMAT = "invalid_format"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    ALREADY_REGISTERED = "already_registered"
    CREATED = "created"


@dataclass
class RegistrationResult:
    outcome: Registration
    participant_id: int | None = None


async def register_code(ctx: AppContext, chat_id: int, raw: str) -> RegistrationResult:
    settings = ctx.settings
    code = normalize_code(raw)
    if not settings.code_pattern.match(code):
        return RegistrationResult(Registration.INVALID_FORMAT)
    async with ctx.db() as session, session.begin():
        current = await by_chat(session, chat_id)
        if current is not None:
            # Повторна доставка апдейту або подвійне надсилання коду.
            return RegistrationResult(Registration.ALREADY_REGISTERED, current.id)
        if settings.code_validation == "whitelist" and await session.get(AllowedCode, code) is None:
            return RegistrationResult(Registration.NOT_FOUND)
        existing = (await session.execute(select(Participant).where(Participant.code == code))).scalar_one_or_none()
        if existing is not None:
            log_event(session, "code_conflict", actor=BOT, participant_id=existing.id, code=code)
            return RegistrationResult(Registration.CONFLICT, existing.id)
        participant = Participant(code=code, last_activity_at=clock.now())
        session.add(participant)
        await session.flush()
        session.add(Contact(participant_id=participant.id, chat_id=chat_id))
        log_event(session, "registered", actor=BOT, participant_id=participant.id)
        return RegistrationResult(Registration.CREATED, participant.id)
