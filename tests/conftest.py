from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.clock import clock
from app.config import Settings
from app.context import AppContext
from app.db.migrate import run_migrations
from app.db.models import Participant, Status
from app.db.session import create_engine, create_sessionmaker

TEST_TOKEN = "123456:TEST-token-not-real"


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "bot_token": TEST_TOKEN,
        "database_path": tmp_path / "study.db",
        "session_secret": "s" * 48,
        "post_survey_url_template": "https://forms.example/view?entry.1={code}",
        "cookie_secure": False,
        "backup_chat_id": None,
        "admin_chat_id": None,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture(autouse=True)
def frozen_clock() -> Iterator[None]:
    clock.freeze(datetime(2026, 3, 2, 8, 0, tzinfo=UTC))
    yield
    clock.reset()


@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    run_migrations(settings.database_path)
    engine = create_engine(settings.database_path)
    yield engine
    await engine.dispose()


@pytest.fixture
def db(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(engine)


@pytest.fixture
def ctx(settings: Settings, engine: AsyncEngine, db: async_sessionmaker[AsyncSession]) -> AppContext:
    return AppContext(settings=settings, engine=engine, db=db)


def open_ctx(settings: Settings) -> AppContext:
    """Окремий контекст на власних налаштуваннях; викликати engine.dispose() у кінці."""
    run_migrations(settings.database_path)
    engine = create_engine(settings.database_path)
    return AppContext(settings=settings, engine=engine, db=create_sessionmaker(engine))


async def add_participant(
    db: async_sessionmaker[AsyncSession], code: str, *, status: str = Status.REGISTERED, **fields: object
) -> int:
    async with db() as s, s.begin():
        participant = Participant(code=code, status=status, **fields)
        s.add(participant)
        await s.flush()
        return participant.id
