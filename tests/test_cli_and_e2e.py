import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import cli
from app.clock import clock
from app.db.models import Contact, Event, Participant, Status
from app.domain.admins import by_username, verify_password
from app.domain.backup import backup_bytes
from app.main import create_app
from app.scheduler.loop import Scheduler
from tests.admin_helpers import OWNER, login, seed_admins
from tests.conftest import activate, close_ctx, get_session, make_settings, open_ctx
from tests.fakes import BotHarness, fake, make_bot

CHAT = 6101


async def test_create_admin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(tmp_path)
    answers = iter(["secret-password-1", "secret-password-1", "a-password-xx", "different-xx-1"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: next(answers))
    assert await cli.run(["create-admin", "--username", "boss", "--role", "owner"], settings) == 0
    assert await cli.run(["create-admin", "--username", "other", "--role", "researcher"], settings) == 1
    ctx = open_ctx(settings)
    async with ctx.read() as s:
        boss = await by_username(s, "boss")
        assert await by_username(s, "other") is None
    assert boss is not None and boss.role == "owner" and verify_password(boss.password_hash, "secret-password-1")
    await close_ctx(ctx)


async def test_backup_and_export_commands(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    ctx = open_ctx(settings)
    await activate(BotHarness(ctx), CHAT, "CLI001")
    await close_ctx(ctx)

    target = tmp_path / "backups"
    target.mkdir()
    assert await cli.run(["backup-now", "--to", str(target)], settings) == 0
    [backup] = list(target.glob("study-*.db"))
    conn = sqlite3.connect(backup)
    assert conn.execute("SELECT code FROM participants").fetchall() == [("CLI001",)]
    conn.close()

    assert await cli.run(["export", "--to", str(tmp_path / "export")], settings) == 0
    assert (tmp_path / "export" / "participants.csv").read_text(encoding="utf-8-sig").startswith("code,group,")


async def test_purge_contacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(tmp_path)
    ctx = open_ctx(settings)
    await activate(BotHarness(ctx), CHAT, "PURGE1")

    monkeypatch.setattr("builtins.input", lambda _prompt: "так")
    assert await cli.run(["purge-contacts", "--confirm"], settings) == 1
    monkeypatch.setattr("builtins.input", lambda _prompt: cli.PURGE_PHRASE)
    assert await cli.run(["purge-contacts"], settings) == 1  # без --confirm
    async with ctx.read() as s:
        assert (await s.execute(select(func.count()).select_from(Contact))).scalar_one() == 1
    assert await cli.run(["purge-contacts", "--confirm"], settings) == 0
    async with ctx.read() as s:
        assert (await s.execute(select(func.count()).select_from(Contact))).scalar_one() == 0
        assert (await s.execute(select(func.count()).select_from(Participant))).scalar_one() == 1
        event = (await s.execute(select(Event).where(Event.type == "contacts_purged"))).scalar_one()
    assert event.payload == {"count": 1}
    await close_ctx(ctx)


async def test_restore_from_backup(tmp_path: Path) -> None:
    """Критерій готовності: з нічного бекапу можна підняти застосунок."""
    live = open_ctx(make_settings(tmp_path / "live"))
    await seed_admins(live)
    await activate(BotHarness(live), CHAT, "RESTORE")
    data = await backup_bytes(live.settings.database_path)
    await close_ctx(live)

    restored_path = tmp_path / "restored" / "study.db"
    restored_path.parent.mkdir()
    restored_path.write_bytes(data)
    settings = make_settings(tmp_path / "restored", database_path=restored_path)
    with TestClient(create_app(settings, run_background=True, bot=make_bot()), follow_redirects=False) as client:
        assert login(client, *OWNER).status_code == 303
        assert "RESTORE" in client.get("/participants").text
        for _ in range(100):
            if client.get("/healthz").status_code == 200:
                break
        assert client.get("/healthz").status_code == 200


async def test_dev_mode_full_path(tmp_path: Path) -> None:
    """Критерій готовності: прискорений час (DEV_DAY_MINUTES) — код → згода → час → старт →
    7 сесій з чек-інами → фінальне посилання."""
    ctx = open_ctx(make_settings(tmp_path, dev_day_minutes=2, start_offset_days=0, code_validation="open"))
    harness, tg = BotHarness(ctx), fake(ctx)
    pid = await activate(harness, CHAT, "FAST01")
    scheduler = Scheduler(ctx)
    scheduler.jobs.pause_seconds = 0
    for day in range(1, 8):
        await scheduler.tick()
        study_session = await get_session(ctx, pid, day)
        assert study_session.sent_at is not None, f"день {day} не надіслано"
        await harness.press(CHAT, f"cs:{study_session.id}")
        await harness.press(CHAT, f"ci:{study_session.id}:did_write:yes")
        await harness.press(CHAT, f"ci:{study_session.id}:mood:{day + 2}")
        clock.advance(timedelta(minutes=2))
    await scheduler.tick()
    async with ctx.read() as s:
        participant = await s.get(Participant, pid)
    assert participant is not None and participant.status == Status.COMPLETED
    assert tg.texts(CHAT)[-1] == ctx.content.t("final_message", url="https://forms.example/view?entry.1=FAST01")
    await close_ctx(ctx)
