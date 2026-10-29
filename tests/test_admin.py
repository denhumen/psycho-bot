import re
from datetime import timedelta

from sqlalchemy import select

from app.clock import clock
from app.config import Settings
from app.context import AppContext
from app.db.models import AllocationSlot, Event, Kind, Message, Participant, ReminderTemplate, Status
from app.scheduler.loop import Scheduler
from tests.admin_helpers import OWNER, RESEARCHER, admin_client, login, post, seed_admins
from tests.conftest import activate, get_session
from tests.fakes import BotHarness

CHAT = 3001


async def test_pages_require_login(settings: Settings, ctx: AppContext) -> None:
    with admin_client(settings) as (client, _):
        for path in ("/", "/participants", "/participants/X", "/templates"):
            response = client.get(path)
            assert response.status_code == 303 and response.headers["location"] == "/login"
        assert client.get("/healthz").status_code == 200
        assert client.get("/login").status_code == 200


async def test_login_and_logout(settings: Settings, ctx: AppContext) -> None:
    await seed_admins(ctx)
    with admin_client(settings) as (client, _):
        assert login(client, "owner", "wrong-password").status_code == 401
        response = login(client, *OWNER)
        assert response.status_code == 303 and response.headers["location"] == "/"
        assert client.get("/").status_code == 200
        assert post(client, "/logout").headers["location"] == "/login"
        assert client.get("/").status_code == 303
    async with ctx.read() as s:
        types = (
            await s.execute(select(Event.type).where(Event.type.like("admin_login%")).order_by(Event.id))
        ).scalars().all()
    assert types == ["admin_login_failed", "admin_login"]


async def test_login_rate_limit_per_username(settings: Settings, ctx: AppContext) -> None:
    await seed_admins(ctx)
    with admin_client(settings) as (client, _):
        for _ in range(5):
            assert login(client, "owner", "bad-password").status_code == 401
        # Навіть правильний пароль відхиляється, поки діє ліміт…
        assert login(client, *OWNER).status_code == 429
        # …але інший логін не постраждав.
        assert login(client, *RESEARCHER).status_code == 303
        post(client, "/logout")
        clock.advance(timedelta(minutes=15, seconds=1))
        assert login(client, *OWNER).status_code == 303


async def test_csrf_is_enforced(settings: Settings, ctx: AppContext) -> None:
    await seed_admins(ctx)
    with admin_client(settings) as (client, _):
        assert client.post("/login", data={"username": "owner", "password": OWNER[1]}).status_code == 403
        login(client, *OWNER)
        assert client.post("/templates", data={"title": "t", "text": "x"}).status_code == 403
        assert client.post("/templates", data={"csrf_token": "forged", "title": "t", "text": "x"}).status_code == 403
        assert post(client, "/templates", {"title": "t", "text": "x"}).status_code == 303
        assert client.post("/logout").status_code == 403


async def test_researcher_cannot_use_owner_actions(settings: Settings, ctx: AppContext) -> None:
    await seed_admins(ctx)
    await activate(BotHarness(ctx), CHAT, "OWNACT")
    with admin_client(settings) as (client, _):
        login(client, *RESEARCHER)
        assert post(client, "/pause", {"paused": "true"}).status_code == 403
        assert post(client, "/participants/OWNACT/withdraw").status_code == 403
        page = client.get("/participants/OWNACT").text
        assert "Позначити як вибув" not in page
    async with ctx.read() as s:
        participant = (await s.execute(select(Participant))).scalar_one()
    assert participant.status == Status.ACTIVE


async def test_owner_pause_and_withdraw(settings: Settings, ctx: AppContext) -> None:
    await seed_admins(ctx)
    await activate(BotHarness(ctx), CHAT, "OWNER2")
    with admin_client(settings) as (client, _):
        login(client, *OWNER)
        assert post(client, "/pause", {"paused": "true"}).status_code == 303
        assert "на паузі" in client.get("/").text
        assert post(client, "/pause", {"paused": "false"}).status_code == 303
        assert post(client, "/participants/OWNER2/withdraw").status_code == 303
    async with ctx.read() as s:
        participant = (await s.execute(select(Participant))).scalar_one()
        events = (await s.execute(select(Event.type, Event.actor).where(Event.actor.like("admin:%")))).all()
    assert participant.status == Status.WITHDRAWN
    assert [t for t, _ in events] == ["admin_login", "scheduler_paused", "scheduler_resumed", "status_changed"]


async def test_dashboard_group_split_only_for_owner(settings: Settings, ctx: AppContext) -> None:
    await seed_admins(ctx)
    harness = BotHarness(ctx)
    for i in range(4):
        await activate(harness, CHAT + i, f"DASH{i:02d}")
    with admin_client(settings) as (client, _):
        login(client, *RESEARCHER)
        page = client.get("/").text
        assert "рандомізовано" in page and "A / B" not in page
        post(client, "/logout")
        login(client, *OWNER)
        page = client.get("/").text
        assert "рандомізовано A / B" in page and "2 / 2" in page


async def test_card_chat_manual_message_and_reply(settings: Settings, ctx: AppContext) -> None:
    await seed_admins(ctx)
    harness = BotHarness(ctx)
    pid = await activate(harness, CHAT, "CHAT01")
    clock.advance(timedelta(days=1, hours=10))
    await Scheduler(ctx).tick()  # інструкція дня 1 у messages
    await harness.text(CHAT, "Маю питання щодо завдання")
    async with ctx.db() as s, s.begin():
        s.add(ReminderTemplate(title="Підбадьорення", text="Як ви? Нагадуємо про опитування."))

    with admin_client(settings) as (client, tg):
        login(client, *RESEARCHER)
        page = client.get("/participants/chat01").text
        assert "📄 Інструкція — день 1" in page
        for group in ("A", "B"):
            assert ctx.content.instructions.text(group, 1) not in page  # інваріант 8
        assert "Маю питання щодо завдання" in page and "bubble in unread" in page
        assert 'data-text="Як ви? Нагадуємо про опитування."' in page
        assert "bubble in unread" not in client.get("/participants/CHAT01").text  # позначено прочитаним

        response = post(client, "/participants/CHAT01/message", {"text": "Дякуємо, відповімо найближчим часом"})
        assert response.status_code == 303
        assert tg.texts(CHAT) == ["Дякуємо, відповімо найближчим часом"]
        page = client.get("/participants/CHAT01").text
        assert "researcher" in page and "Повідомлення надіслано." in page

    # Відповідь учасника з'являється в чаті.
    await harness.text(CHAT, "Дякую!")
    with admin_client(settings) as (client, _):
        login(client, *RESEARCHER)
        assert "Дякую!" in client.get("/participants/CHAT01").text
    async with ctx.read() as s:
        manual = (await s.execute(select(Message).where(Message.kind == Kind.MANUAL))).scalar_one()
        audit = (await s.execute(select(Event).where(Event.type == "admin_message_sent"))).scalar_one()
    assert manual.sender_admin_id is not None and manual.participant_id == pid
    assert audit.participant_id == pid and audit.actor.startswith("admin:")


async def test_no_manual_messages_to_withdrawn(settings: Settings, ctx: AppContext) -> None:
    await seed_admins(ctx)
    harness = BotHarness(ctx)
    await activate(harness, CHAT, "GONE01")
    await harness.press(CHAT, "stop:yes")
    with admin_client(settings) as (client, tg):
        login(client, *RESEARCHER)
        page = client.get("/participants/GONE01").text
        assert "надсилати повідомлення не можна" in page and 'name="text"' not in page
        post(client, "/participants/GONE01/message", {"text": "Привіт"})
        assert tg.texts() == []


async def test_participant_filters(settings: Settings, ctx: AppContext) -> None:
    await seed_admins(ctx)
    harness = BotHarness(ctx)
    pending = await activate(harness, CHAT, "PEND01")
    streak = await activate(harness, CHAT + 1, "MISS02")
    await harness.text(CHAT + 2, "WAIT03")  # лише зареєстрований
    scheduler = Scheduler(ctx)
    clock.advance(timedelta(days=1, hours=10))
    await scheduler.tick()  # день 1
    clock.advance(timedelta(days=1))
    await scheduler.tick()  # день 2; день 1 пропущено
    s2 = await get_session(ctx, pending, 2)
    await harness.press(CHAT, f"ci:{s2.id}:did_write:yes")
    await harness.press(CHAT, f"ci:{s2.id}:mood:6")
    clock.advance(timedelta(days=1))
    await scheduler.tick()  # день 3; у MISS02 пропущено 1 і 2, у PEND01 — лише 1
    await harness.text(CHAT, "привіт")

    with admin_client(settings) as (client, _):
        login(client, *RESEARCHER)

        def codes(query: str) -> set[str]:
            page = client.get(f"/participants?{query}").text
            return {code for code in ("PEND01", "MISS02", "WAIT03") if f">{code}<" in page}

        assert codes("") == {"PEND01", "MISS02", "WAIT03"}
        assert codes("flag=not_done_today") == {"PEND01", "MISS02"}
        assert codes("flag=missed_streak") == {"MISS02"}
        assert codes("flag=unread") == {"PEND01"}
        assert codes("flag=not_started") == {"WAIT03"}
        assert codes("status=registered") == {"WAIT03"}
        assert codes("q=miss") == {"MISS02"}
        page = client.get("/participants?q=PEND").text
        cells = re.findall(r'<span title="День \d: [^"]+">(.)</span>', page)
        assert "3/7" in page and "".join(cells) == "✕●○····"


async def test_group_never_rendered(settings: Settings, ctx: AppContext) -> None:
    """Інваріант 1: учасники з однаковою історією, але різними групами, дають однаковий HTML."""
    await seed_admins(ctx)
    harness = BotHarness(ctx)
    for i in range(4):
        await activate(harness, CHAT + i, f"SAME{i}")
    clock.advance(timedelta(days=1, hours=10))
    await Scheduler(ctx).tick()
    async with ctx.read() as s:
        groups = dict((await s.execute(
            select(Participant.code, AllocationSlot.group).join(AllocationSlot, AllocationSlot.participant_id == Participant.id)
        )).tuples().all())
    code_a = next(code for code, group in groups.items() if group == "A")
    code_b = next(code for code, group in groups.items() if group == "B")
    instruction_texts = [ctx.content.instructions.text(g, d) for g in ("A", "B") for d in range(1, 8)]

    with admin_client(settings) as (client, _):
        for user in (OWNER, RESEARCHER):
            login(client, *user)
            card_a = client.get(f"/participants/{code_a}").text.replace(code_a, "CODE")
            card_b = client.get(f"/participants/{code_b}").text.replace(code_b, "CODE")
            assert card_a == card_b
            listing = client.get("/participants").text
            row_a = listing[listing.index(f">{code_a}<"):].split("</tr>")[0].replace(code_a, "CODE")
            row_b = listing[listing.index(f">{code_b}<"):].split("</tr>")[0].replace(code_b, "CODE")
            assert row_a == row_b
            for page in (card_a, listing, client.get("/").text, client.get("/templates").text):
                assert not any(text in page for text in instruction_texts)
            post(client, "/logout")


async def test_templates_crud(settings: Settings, ctx: AppContext) -> None:
    await seed_admins(ctx)
    with admin_client(settings) as (client, _):
        login(client, *RESEARCHER)
        post(client, "/templates", {"title": "Перший", "text": "Текст 1", "is_active": "on"})
        async with ctx.read() as s:
            item = (await s.execute(select(ReminderTemplate))).scalar_one()
        post(client, f"/templates/{item.id}", {"title": "Змінений", "text": "Текст 2"})
        async with ctx.read() as s:
            item = (await s.execute(select(ReminderTemplate))).scalar_one()
        assert (item.title, item.text, item.is_active) == ("Змінений", "Текст 2", False)
        assert "Змінений" in client.get("/templates").text
        post(client, f"/templates/{item.id}/delete")
        async with ctx.read() as s:
            assert (await s.execute(select(ReminderTemplate))).scalars().all() == []
