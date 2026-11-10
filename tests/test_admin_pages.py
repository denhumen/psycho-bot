"""Нові сторінки редизайну: peek, пагінація, редактор шаблонів, реєстр кодів, експорт, бекап."""

import re
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from app.clock import clock
from app.config import PROJECT_ROOT, Settings
from app.context import AppContext
from app.db.models import AllocationSlot, AllowedCode, Participant, ReminderTemplate
from app.scheduler.loop import Scheduler
from tests.admin_helpers import OWNER, RESEARCHER, admin_client, login, post, seed_admins
from tests.conftest import activate, add_participant, close_ctx, make_settings, open_ctx
from tests.fakes import BotHarness

CHAT = 3501


async def test_peek_fragment_and_send_from_peek(settings: Settings, ctx: AppContext) -> None:
    await seed_admins(ctx)
    harness = BotHarness(ctx)
    await activate(harness, CHAT, "PEEK01")
    await harness.text(CHAT, "Питання з peek")
    with admin_client(settings) as (client, tg):
        login(client, *RESEARCHER)
        fragment = client.get("/participants/PEEK01/peek?back=/participants%3Fflag%3Dunread").text
        assert fragment.lstrip().startswith("<aside class=\"peek\"")
        assert "Питання з peek" in fragment and "msg in unread" in fragment
        assert 'value="/participants?flag=unread&amp;peek=PEEK01"' in fragment
        # Відкриття peek позначає прочитаним.
        assert "msg in unread" not in client.get("/participants/PEEK01/peek").text

        response = post(client, "/participants/PEEK01/message", {"text": "Відповідь", "next": "/participants?peek=PEEK01"})
        assert response.headers["location"] == "/participants?peek=PEEK01"
        assert tg.texts(CHAT)[-1] == "Відповідь"
        page = client.get("/participants?peek=PEEK01").text
        assert 'data-peek="PEEK01"' in page and 'class="sel"' in page

        # Зовнішній next ігнорується (open redirect).
        response = post(client, "/participants/PEEK01/message", {"text": "Ще", "next": "https://evil.example/x"})
        assert response.headers["location"] == "/participants/PEEK01"
        response = post(client, "/participants/PEEK01/message", {"text": "Ще", "next": "//evil.example"})
        assert response.headers["location"] == "/participants/PEEK01"


async def test_participants_pagination(settings: Settings, ctx: AppContext) -> None:
    await seed_admins(ctx)
    for i in range(60):
        await add_participant(ctx.db, f"PG{i:03d}")
    with admin_client(settings) as (client, _):
        login(client, *RESEARCHER)
        first = client.get("/participants").text
        assert "1–50 з 60" in first and 'href="/participants?page=2"' in first
        second = client.get("/participants?page=2").text
        assert "51–60 з 60" in second
        assert len(re.findall(r'class="code">PG\d{3}<', second)) == 10
        assert "51–60 з 60" in client.get("/participants?page=99").text  # за межами — остання сторінка


async def test_template_editor_pages(settings: Settings, ctx: AppContext) -> None:
    await seed_admins(ctx)
    with admin_client(settings) as (client, _):
        login(client, *RESEARCHER)
        assert client.get("/templates/new").status_code == 200
        assert post(client, "/templates", {"title": "Привітання", "text": "Добрий день!", "is_active": "on"}).status_code == 303
        async with ctx.read() as s:
            item = (await s.execute(select(ReminderTemplate))).scalar_one()
        page = client.get(f"/templates/{item.id}").text
        assert "Так побачить учасник" in page and "Добрий день!" in page and 'action="/templates/' in page
        assert client.get("/templates/9999").status_code == 404
        listing = client.get("/templates").text
        assert "Показується" in listing and f'href="/templates/{item.id}"' in listing


async def test_codes_registry_filters(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, code_validation="whitelist")
    ctx = open_ctx(settings)
    await seed_admins(ctx)
    async with ctx.db() as s, s.begin():
        s.add_all([AllowedCode(code="FREE01", uploaded_by="owner"), AllowedCode(code="USED01", uploaded_by="owner")])
    await BotHarness(ctx).text(CHAT, "used01")
    with admin_client(settings) as (client, _):
        login(client, *OWNER)
        assert "Завантажити коди" in client.get("/codes/new").text

        def codes(query: str) -> set[str]:
            page = client.get(f"/codes{query}").text
            return {c for c in ("FREE01", "USED01") if f">{c}<" in page}

        assert codes("") == {"FREE01", "USED01"}
        assert codes("?state=free") == {"FREE01"}
        assert codes("?state=used") == {"USED01"}
        assert codes("?q=free") == {"FREE01"}
        # Спроби з іншого акаунта (конфлікти) на сторінці кодів не показуються.
        assert "конфлікт" not in client.get("/codes").text.lower()
        post(client, "/logout")
        login(client, *RESEARCHER)
        assert client.get("/codes/new").status_code == 403
        assert client.get("/users/new").status_code == 403
    await close_ctx(ctx)


async def test_export_counts_and_backup_history(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, backup_chat_id=-100321)
    ctx = open_ctx(settings)
    await seed_admins(ctx)
    await activate(BotHarness(ctx), CHAT, "EXPCNT")
    with admin_client(settings) as (client, _):
        login(client, *OWNER)
        page = client.get("/export").text
        rows = dict(re.findall(r'<td class="mono">([a-z_]+\.csv)</td><td>.*?</td><td class="num">([\d ]+)</td>', page))
        assert rows["participants.csv"] == "1" and rows["sessions.csv"] == "7"
        post(client, "/backup/send")
        page = client.get("/backup").text
        assert "щоночі о 03:00" in page and "BACKUP_TIME" not in page
        assert "Копія в Telegram, вручну" in page and ">owner<" in page
    await close_ctx(ctx)


async def test_group_never_in_peek(settings: Settings, ctx: AppContext) -> None:
    await seed_admins(ctx)
    harness = BotHarness(ctx)
    for i in range(4):
        await activate(harness, CHAT + i, f"PKG{i}")
    clock.advance(timedelta(days=1, hours=10))
    await Scheduler(ctx).tick()
    async with ctx.read() as s:
        groups = dict((await s.execute(
            select(Participant.code, AllocationSlot.group).join(AllocationSlot, AllocationSlot.participant_id == Participant.id)
        )).tuples().all())
    code_a = next(c for c, g in groups.items() if g == "A")
    code_b = next(c for c, g in groups.items() if g == "B")
    with admin_client(settings) as (client, _):
        login(client, *OWNER)
        peek_a = client.get(f"/participants/{code_a}/peek").text.replace(code_a, "CODE")
        peek_b = client.get(f"/participants/{code_b}/peek").text.replace(code_b, "CODE")
        assert peek_a == peek_b


def test_no_middle_dot_in_templates() -> None:
    """Правило дизайну: різні факти не з'єднуються через «·»."""
    for path in (PROJECT_ROOT / "app" / "admin" / "templates").glob("*.html"):
        assert "·" not in path.read_text(encoding="utf-8"), path.name
