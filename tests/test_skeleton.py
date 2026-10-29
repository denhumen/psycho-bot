from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import select, text

from app.config import Settings
from app.db.migrate import run_migrations
from app.db.models import Base, Participant
from app.main import create_app
from tests.conftest import make_settings
from tests.fakes import make_bot


def test_settings_parse_lists_and_empty_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REMINDER_TIME_OPTIONS", " 09:00, 21:30 ")
    monkeypatch.setenv("BACKUP_CHAT_ID", "")
    monkeypatch.setenv("ADMIN_CHAT_ID", "-100123")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.reminder_times == ["09:00", "21:30"]
    assert s.backup_chat_id is None
    assert s.admin_chat_id == -100123


@pytest.mark.parametrize(
    "overrides",
    [
        {"reminder_time_options": "9:00"},
        {"backup_time": "25:00"},
        {"allocation_block_size": 3},
        {"post_survey_url_template": "https://example.com/no-code"},
        {"timezone": "Mars/Base"},
    ],
)
def test_settings_reject_invalid(tmp_path: Path, overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_settings(tmp_path, **overrides)


def test_runtime_check_requires_secrets(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="BOT_TOKEN"):
        make_settings(tmp_path, bot_token="").check_runtime()


def test_migrations_match_models(tmp_path: Path) -> None:
    db_path = tmp_path / "m.db"
    run_migrations(db_path)
    run_migrations(db_path)  # повторний upgrade head — no-op
    engine = create_sync_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == []


async def test_engine_pragmas_and_utc_roundtrip(db) -> None:  # type: ignore[no-untyped-def]
    async with db() as s:
        assert (await s.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
        assert (await s.execute(text("PRAGMA foreign_keys"))).scalar() == 1
        assert (await s.execute(text("PRAGMA busy_timeout"))).scalar() == 5000
        moment = datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
        s.add(Participant(code="ABCD", consented_at=moment))
        await s.commit()
    async with db() as s:
        p = (await s.execute(select(Participant))).scalar_one()
        assert p.consented_at == moment
        assert p.consented_at.tzinfo is not None
        assert p.created_at.tzinfo is not None


def test_healthz(settings: Settings) -> None:
    app = create_app(settings, run_background=False, bot=make_bot())
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
