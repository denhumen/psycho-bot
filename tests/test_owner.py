import csv
import io
import sqlite3
import zipfile
from datetime import timedelta
from pathlib import Path

from aiogram.methods import SendDocument
from sqlalchemy import select

from app.clock import clock
from app.context import AppContext
from app.db.models import AllowedCode, Event
from app.domain.state import LAST_BACKUP_AT, get_time
from app.scheduler.loop import Scheduler
from tests.admin_helpers import OWNER, RESEARCHER, admin_client, login, post, seed_admins
from tests.conftest import activate, close_ctx, get_session, make_settings, open_ctx
from tests.fakes import BotHarness

OWNER_PAGES = ("/codes", "/export", "/backup", "/users")
OWNER_POSTS = ("/codes", "/export", "/backup/download", "/backup/send", "/users", "/users/1/active", "/users/1/password")


def read_csv(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    text = archive.read(name).decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


async def test_researcher_has_no_owner_pages(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    ctx = open_ctx(settings)
    await seed_admins(ctx)
    with admin_client(settings) as (client, _):
        login(client, *RESEARCHER)
        for path in OWNER_PAGES:
            assert client.get(path).status_code == 403, path
        for path in OWNER_POSTS:
            assert post(client, path, {"active": "false", "password": "x" * 12}).status_code == 403, path
        page = client.get("/").text
        assert 'href="/export"' not in page and 'href="/codes"' not in page
    async with ctx.read() as s:
        assert (await s.execute(select(Event).where(Event.type == "export_downloaded"))).first() is None
    await close_ctx(ctx)


async def test_codes_upload_and_whitelist(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, code_validation="whitelist")
    ctx = open_ctx(settings)
    await seed_admins(ctx)
    with admin_client(settings) as (client, _):
        login(client, *OWNER)
        post(client, "/codes", {"codes": " abc123 \nABC123\nbad code\nXY\nКОД_01\n\n"})
        page = client.get("/codes").text
        assert "додано 2, дублікатів 1, невалідних 2" in page
        csv_file = "code,email\nfile01,a@x\nabc123,b@x\n".encode()
        post(client, "/codes", files={"file": ("codes.csv", csv_file, "text/csv")})
        page = client.get("/codes").text
        # Рядок заголовка «code» пропускається.
        assert "додано 1, дублікатів 1, невалідних 0" in page
    async with ctx.read() as s:
        codes = set((await s.execute(select(AllowedCode.code))).scalars())
        uploaded = (await s.execute(select(Event.payload).where(Event.type == "codes_uploaded"))).scalars().all()
    assert codes == {"ABC123", "КОД_01", "FILE01"}
    assert uploaded[0] == {"added": 2, "duplicates": 1, "invalid": 2}

    harness = BotHarness(ctx)
    await harness.text(5555, "file01")
    with admin_client(settings) as (client, _):
        login(client, *OWNER)
        page = client.get("/codes").text
        assert '<div class="value">3</div><div class="label">усього кодів</div>' in page
        assert '<div class="value">1</div><div class="label">використано</div>' in page
    await close_ctx(ctx)


async def test_export_zip(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    ctx = open_ctx(settings)
    await seed_admins(ctx)
    harness = BotHarness(ctx)
    pid = await activate(harness, 2001, "EXP001")
    clock.advance(timedelta(days=1, hours=10))
    await Scheduler(ctx).tick()
    s1 = await get_session(ctx, pid, 1)
    await harness.press(2001, f"ci:{s1.id}:did_write:partly")
    await harness.press(2001, f"ci:{s1.id}:mood:6")
    await harness.text(2001, "питання")

    with admin_client(settings) as (client, _):
        login(client, *OWNER)
        response = post(client, "/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert sorted(archive.namelist()) == sorted(
        ["participants.csv", "sessions.csv", "checkin_answers.csv", "checkin_answers_wide.csv", "messages.csv", "events.csv"]
    )
    participants = read_csv(archive, "participants.csv")
    assert participants[0]["code"] == "EXP001" and participants[0]["group"] in ("A", "B")
    assert participants[0]["randomized_at"].endswith("+02:00")
    assert len(read_csv(archive, "sessions.csv")) == 7
    wide = read_csv(archive, "checkin_answers_wide.csv")
    assert len(wide) == 7
    assert (wide[0]["day"], wide[0]["did_write"], wide[0]["mood"]) == ("1", "partly", "6")
    assert wide[1]["did_write"] == ""
    assert len(read_csv(archive, "checkin_answers.csv")) == 2
    messages = read_csv(archive, "messages.csv")
    instruction = [m for m in messages if m["kind"] == "instruction"]
    assert len(instruction) == 1 and instruction[0]["text"] == ""
    assert any(m["text"] == "питання" for m in messages)
    assert "chat_id" not in archive.read("messages.csv").decode("utf-8-sig")
    events = read_csv(archive, "events.csv")
    assert {"randomized", "status_changed"} <= {e["type"] for e in events}
    async with ctx.read() as s:
        exported = (await s.execute(select(Event).where(Event.type == "export_downloaded"))).scalars().all()
    assert len(exported) == 1 and exported[0].actor.startswith("admin:")
    await close_ctx(ctx)


async def test_backup_download_and_send(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, backup_chat_id=-100999)
    ctx = open_ctx(settings)
    await seed_admins(ctx)
    await activate(BotHarness(ctx), 2002, "BAK001")
    with admin_client(settings) as (client, tg):
        login(client, *OWNER)
        response = post(client, "/backup/download")
        assert response.status_code == 200
        copy = tmp_path / "copy.db"
        copy.write_bytes(response.content)
        conn = sqlite3.connect(copy)
        assert conn.execute("SELECT code FROM participants").fetchall() == [("BAK001",)]
        conn.close()

        assert post(client, "/backup/send").status_code == 303
        docs = [c for c in tg.calls if isinstance(c, SendDocument)]
        assert len(docs) == 1 and docs[0].chat_id == -100999
        assert "Бекап надіслано" in client.get("/backup").text
    async with ctx.read() as s:
        assert await get_time(s, LAST_BACKUP_AT) == clock.now()
    await close_ctx(ctx)


async def test_users_management(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    ctx = open_ctx(settings)
    await seed_admins(ctx)
    with admin_client(settings) as (owner, _):
        login(owner, *OWNER)
        post(owner, "/users", {"username": "newbie", "password": "short", "role": "researcher"})
        assert "щонайменше 10" in owner.get("/users").text
        post(owner, "/users", {"username": "newbie", "password": "long-enough-1", "role": "researcher"})
        page = owner.get("/users").text
        assert "Користувача newbie створено" in page

        with admin_client(settings) as (newbie, _):
            assert login(newbie, "newbie", "long-enough-1").status_code == 303
            assert newbie.get("/").status_code == 200
            async with ctx.read() as s:
                from app.domain.admins import by_username

                user = await by_username(s, "newbie")
                me = await by_username(s, "owner")
            assert user is not None and me is not None
            post(owner, f"/users/{user.id}/active", {"active": "false"})
            # Активна сесія деактивованого користувача більше не працює.
            assert newbie.get("/").status_code == 303
            assert login(newbie, "newbie", "long-enough-1").status_code == 401

        post(owner, f"/users/{me.id}/active", {"active": "false"})
        assert "Не можна деактивувати самого себе" in owner.get("/users").text
        post(owner, f"/users/{user.id}/active", {"active": "true"})
        post(owner, f"/users/{user.id}/password", {"password": "brand-new-pass"})
        with admin_client(settings) as (newbie, _):
            assert login(newbie, "newbie", "long-enough-1").status_code == 401
            assert login(newbie, "newbie", "brand-new-pass").status_code == 303
    await close_ctx(ctx)
