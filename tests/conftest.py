from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.sender import Sender
from app.clock import clock
from app.config import Settings
from app.context import AppContext
from app.db.migrate import run_migrations
from app.db.models import Participant, Status, StudySession
from app.db.session import create_engine, create_read_sessionmaker, create_sessionmaker
from app.domain.content import load_content
from tests.fakes import TOKEN, BotHarness, FakeTelegram, fake, make_bot


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "bot_token": TOKEN,
        "database_path": tmp_path / "study.db",
        "session_secret": "s" * 48,
        "post_survey_url_template": "https://forms.example/view?entry.1={code}",
        "cookie_secure": False,
        "backup_chat_id": None,
        "admin_chat_id": None,
        "code_validation": "open",
        "support_contact": "@support_test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def open_ctx(settings: Settings) -> AppContext:
    """Контекст «процесу»: власний engine і фейковий бот. Два open_ctx на одну БД = рестарт.
    Викликати close_ctx у кінці."""
    run_migrations(settings.database_path)
    engine = create_engine(settings.database_path)
    db = create_sessionmaker(engine)
    content = load_content(settings.content_dir, settings.support_contact)
    return AppContext(
        settings=settings,
        engine=engine,
        db=db,
        read=create_read_sessionmaker(engine),
        content=content,
        sender=Sender(make_bot(), db, content),
    )


async def close_ctx(ctx: AppContext) -> None:
    await ctx.engine.dispose()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture(autouse=True)
def frozen_clock() -> Iterator[None]:
    # 2026-03-02 08:00 UTC = 10:00 за Києвом (зимовий час).
    clock.freeze(datetime(2026, 3, 2, 8, 0, tzinfo=UTC))
    yield
    clock.reset()


@pytest.fixture
async def ctx(settings: Settings) -> AsyncIterator[AppContext]:
    context = open_ctx(settings)
    yield context
    await close_ctx(context)


@pytest.fixture
def db(ctx: AppContext) -> async_sessionmaker[AsyncSession]:
    return ctx.db


@pytest.fixture
def tg(ctx: AppContext) -> FakeTelegram:
    return fake(ctx)


@pytest.fixture
def harness(ctx: AppContext) -> BotHarness:
    return BotHarness(ctx)


async def activate(harness: BotHarness, chat_id: int, code: str, time: str = "20:00") -> int:
    """Повна реєстрація через бота: код → згода → час → «Готовий почати»."""
    await harness.text(chat_id, code)
    await harness.press(chat_id, "consent:yes")
    await harness.press(chat_id, f"time:{time}")
    await harness.press(chat_id, "ready")
    async with harness.ctx.read() as s:
        participant = (await s.execute(select(Participant).where(Participant.code == code.strip().upper()))).scalar_one()
    assert participant.status == Status.ACTIVE
    return participant.id


async def get_session(ctx: AppContext, participant_id: int, day: int) -> StudySession:
    async with ctx.read() as s:
        return (
            await s.execute(
                select(StudySession).where(StudySession.participant_id == participant_id, StudySession.day == day)
            )
        ).scalar_one()


async def mark_sent(ctx: AppContext, participant_id: int, day: int) -> StudySession:
    """Імітує надіслану планувальником інструкцію (сам планувальник — етап 5)."""
    async with ctx.db() as s, s.begin():
        study_session = (
            await s.execute(
                select(StudySession).where(StudySession.participant_id == participant_id, StudySession.day == day)
            )
        ).scalar_one()
        study_session.sent_at = clock.now()
    return study_session


async def add_participant(
    db: async_sessionmaker[AsyncSession], code: str, *, status: str = Status.REGISTERED, **fields: object
) -> int:
    async with db() as s, s.begin():
        participant = Participant(code=code, status=status, **fields)
        s.add(participant)
        await s.flush()
        return participant.id
