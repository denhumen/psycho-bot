import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendDocument, SendMessage
from sqlalchemy import select

from app.clock import clock
from app.context import AppContext
from app.db.models import Event, Kind, Message, Participant, Status, StudySession
from app.domain.allocation import group_of
from app.domain.state import LAST_BACKUP_AT, SCHEDULER_PAUSED, get_time, set_value
from app.scheduler.loop import Scheduler
from tests.conftest import activate, close_ctx, get_session, make_settings, open_ctx
from tests.fakes import BotHarness, FakeTelegram, fake

CHAT = 4001
DAY1 = datetime(2026, 3, 3, 18, 0, tzinfo=UTC)  # 03.03 20:00 за Києвом


def make_scheduler(ctx: AppContext) -> Scheduler:
    scheduler = Scheduler(ctx)
    scheduler.jobs.pause_seconds = 0
    return scheduler


async def instruction_for(ctx: AppContext, pid: int, day: int) -> str:
    async with ctx.read() as s:
        group = await group_of(s, pid)
    assert group is not None
    return ctx.content.instructions.text(group, day)


async def set_paused(ctx: AppContext, paused: bool) -> None:
    async with ctx.db() as s, s.begin():
        await set_value(s, SCHEDULER_PAUSED, "true" if paused else "false")


async def events(ctx: AppContext, type_: str) -> list[Event]:
    async with ctx.read() as s:
        return list((await s.execute(select(Event).where(Event.type == type_))).scalars())


async def complete_checkin(harness: BotHarness, session_id: int) -> None:
    await harness.press(CHAT, f"ci:{session_id}:did_write:yes")
    await harness.press(CHAT, f"ci:{session_id}:mood:8")


async def test_send_due_sends_once(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    pid = await activate(harness, CHAT, "SEND01")
    scheduler = make_scheduler(ctx)
    tg.clear()
    clock.freeze(DAY1 - timedelta(minutes=1))
    await scheduler.tick()
    assert tg.texts(CHAT) == []

    clock.freeze(DAY1)
    await scheduler.tick()
    await scheduler.tick()
    await make_scheduler(ctx).tick()  # «новий процес»
    expected = await instruction_for(ctx, pid, 1)
    assert tg.texts(CHAT) == [expected]
    s1 = await get_session(ctx, pid, 1)
    assert tg.last_markup(CHAT).inline_keyboard[0][0].callback_data == f"cs:{s1.id}"
    assert s1.sent_at == DAY1
    async with ctx.read() as s:
        logged = (await s.execute(select(Message).where(Message.kind == Kind.INSTRUCTION))).scalars().all()
    assert len(logged) == 1 and logged[0].session_day == 1


async def test_pause_blocks_everything_until_resumed(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    pid = await activate(harness, CHAT, "PAUSE1")
    scheduler = make_scheduler(ctx)
    clock.freeze(DAY1)
    await scheduler.tick()  # день 1 надіслано
    await set_paused(ctx, True)
    tg.clear()
    clock.freeze(DAY1 + timedelta(days=1, minutes=5))  # дедлайн дня 1 минув, день 2 настав
    await scheduler.tick()
    assert tg.texts() == []
    s1 = await get_session(ctx, pid, 1)
    assert s1.missed_at is None  # на паузі сесії не закриваються

    await set_paused(ctx, False)
    await scheduler.tick()
    assert (await get_session(ctx, pid, 1)).missed_at is not None
    assert tg.texts(CHAT) == [await instruction_for(ctx, pid, 2)]


async def test_downtime_marks_missed_instead_of_sending(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    pid = await activate(harness, CHAT, "DOWN01")
    tg.clear()
    # Процес лежав 2,5 доби: дні 1 і 2 повністю пропущені, день 3 ще відкритий.
    clock.freeze(DAY1 + timedelta(days=2, hours=12))
    await make_scheduler(ctx).tick()
    assert tg.texts(CHAT) == [await instruction_for(ctx, pid, 3)]
    for day in (1, 2):
        study_session = await get_session(ctx, pid, day)
        assert study_session.sent_at is None and study_session.missed_at is not None
    payloads = [e.payload for e in await events(ctx, "session_missed")]
    assert payloads == [{"day": 1, "sent": False}, {"day": 2, "sent": False}]


async def test_blocked_participant_is_skipped(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    pid = await activate(harness, CHAT, "BLOCK3")
    tg.blocked.add(CHAT)
    scheduler = make_scheduler(ctx)
    clock.freeze(DAY1)
    await scheduler.tick()
    await scheduler.tick()
    sends = [c for c in tg.sent(CHAT) if c.text == await instruction_for(ctx, pid, 1)]
    assert len(sends) == 1  # одна спроба → Forbidden → більше не пробуємо
    async with ctx.read() as s:
        participant = await s.get(Participant, pid)
    assert participant is not None and participant.bot_blocked
    clock.advance(timedelta(days=1))
    await scheduler.tick()
    assert (await get_session(ctx, pid, 1)).missed_at is not None


async def test_transient_failure_is_retried_later(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    pid = await activate(harness, CHAT, "RETRY1")
    scheduler = make_scheduler(ctx)
    scheduler.jobs.ctx.sender.sleep = _no_sleep
    clock.freeze(DAY1)
    error = TelegramNetworkError(method=SendMessage(chat_id=CHAT, text="x"), message="down")
    tg.fail_next += [error] * 4  # усі спроби sender'а в цьому тіку
    await scheduler.tick()
    assert (await get_session(ctx, pid, 1)).sent_at is None
    await scheduler.tick()  # одразу не повторюємо
    assert (await get_session(ctx, pid, 1)).sent_at is None
    clock.advance(timedelta(minutes=10))
    await scheduler.tick()
    assert (await get_session(ctx, pid, 1)).sent_at is not None
    assert len(await events(ctx, "send_failed")) == 1


async def _no_sleep(_: float) -> None:
    return None


async def test_crash_between_send_and_commit_is_not_lost(harness: BotHarness, ctx: AppContext) -> None:
    """At-least-once: якщо процес помер після відповіді Telegram, але до коміту sent_at,
    після рестарту інструкцію буде надіслано знову (див. DECISIONS.md) — але не втрачено."""
    pid = await activate(harness, CHAT, "CRASH2")
    clock.freeze(DAY1)
    scheduler = make_scheduler(ctx)

    async def killed(_session: object) -> None:
        raise RuntimeError("process killed before commit")

    original_send = ctx.sender.send

    async def send_then_die(*args: object, **kwargs: object) -> object:
        kwargs["after"] = killed
        return await original_send(*args, **kwargs)  # type: ignore[arg-type]

    ctx.sender.send = send_then_die  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await scheduler.jobs.send_due()  # без tick: виняток «вбиває процес»
    assert (await get_session(ctx, pid, 1)).sent_at is None

    restarted = open_ctx(ctx.settings)
    await make_scheduler(restarted).tick()
    assert fake(restarted).texts(CHAT) == [await instruction_for(restarted, pid, 1)]
    assert (await get_session(restarted, pid, 1)).sent_at is not None
    await close_ctx(restarted)


async def test_auto_reminder(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    pid = await activate(harness, CHAT, "REMIND")
    scheduler = make_scheduler(ctx)
    clock.freeze(DAY1)
    await scheduler.tick()
    tg.clear()
    clock.advance(timedelta(hours=2, minutes=59))
    await scheduler.tick()
    assert tg.texts(CHAT) == []
    clock.advance(timedelta(minutes=1))
    await scheduler.tick()
    await scheduler.tick()
    assert tg.texts(CHAT) == [ctx.content.t("auto_reminder")]
    s1 = await get_session(ctx, pid, 1)
    assert s1.reminder_sent_at == clock.now()
    assert tg.last_markup(CHAT).inline_keyboard[0][0].callback_data == f"cs:{s1.id}"


async def test_no_reminder_after_checkin_or_when_disabled(tmp_path: Path) -> None:
    for hours, complete in ((3, True), (0, False)):
        ctx = open_ctx(make_settings(tmp_path / f"h{hours}", auto_reminder_after_hours=hours))
        harness, tg = BotHarness(ctx), fake(ctx)
        pid = await activate(harness, CHAT, "NOREM1")
        scheduler = make_scheduler(ctx)
        clock.freeze(DAY1)
        await scheduler.tick()
        if complete:
            await complete_checkin(harness, (await get_session(ctx, pid, 1)).id)
        clock.advance(timedelta(hours=5))
        await scheduler.tick()
        assert ctx.content.t("auto_reminder") not in tg.texts(CHAT)
        await close_ctx(ctx)


async def test_full_week_and_final_link(tmp_path: Path) -> None:
    ctx = open_ctx(make_settings(tmp_path))
    harness, tg = BotHarness(ctx), fake(ctx)
    pid = await activate(harness, CHAT, "тиждень1")  # кирилиця → URL-encoding у посиланні
    scheduler = make_scheduler(ctx)
    for day in range(1, 8):
        clock.freeze(DAY1 + timedelta(days=day - 1))
        await scheduler.tick()
        if day != 4:  # день 4 пропущено
            await complete_checkin(harness, (await get_session(ctx, pid, day)).id)
    # День 7 пройдено → наступний тік завершує участь; подальші тіки нічого не дублюють.
    await scheduler.tick()
    await scheduler.tick()
    async with ctx.read() as s:
        participant = await s.get(Participant, pid)
        sessions = (await s.execute(select(StudySession).where(StudySession.participant_id == pid))).scalars().all()
    assert participant is not None and participant.status == Status.COMPLETED
    assert participant.post_survey_sent_at is not None
    url = "https://forms.example/view?entry.1=%D0%A2%D0%98%D0%96%D0%94%D0%95%D0%9D%D0%AC1"
    finals = [t for t in tg.texts(CHAT) if url in t]
    assert finals == [ctx.content.t("final_message", url=url)]
    assert sum(s.completed_at is not None for s in sessions) == 6
    assert [s.day for s in sessions if s.missed_at is not None] == [4]
    await close_ctx(ctx)


async def test_final_message_survives_failure(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    pid = await activate(harness, CHAT, "FINAL1")
    scheduler = make_scheduler(ctx)
    ctx.sender.sleep = _no_sleep
    clock.freeze(DAY1 + timedelta(days=7, minutes=1))  # усе прострочено
    error = TelegramNetworkError(method=SendMessage(chat_id=CHAT, text="x"), message="down")
    tg.fail_next += [error] * 4
    await scheduler.tick()
    async with ctx.read() as s:
        participant = await s.get(Participant, pid)
    assert participant is not None and participant.status == Status.COMPLETED
    assert participant.post_survey_sent_at is None
    clock.advance(timedelta(minutes=10))
    await scheduler.tick()
    async with ctx.read() as s:
        participant = await s.get(Participant, pid)
    assert participant is not None and participant.post_survey_sent_at is not None


async def test_withdrawn_gets_nothing(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    pid = await activate(harness, CHAT, "WITHD1")
    await harness.press(CHAT, "stop:yes")
    tg.clear()
    for day in range(9):
        clock.freeze(DAY1 + timedelta(days=day, hours=4))
        await make_scheduler(ctx).tick()
    assert tg.texts(CHAT) == []
    async with ctx.read() as s:
        participant = await s.get(Participant, pid)
    assert participant is not None and participant.status == Status.WITHDRAWN


async def test_backup_once_per_day_and_not_after_restart(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, backup_chat_id=-100777)
    ctx = open_ctx(settings)
    tg = fake(ctx)
    clock.freeze(datetime(2026, 3, 2, 0, 30, tzinfo=UTC))  # 02:30 за Києвом, BACKUP_TIME 03:00
    await make_scheduler(ctx).tick()
    docs = [c for c in tg.calls if isinstance(c, SendDocument)]
    assert len(docs) == 1  # перший запуск: бекапу ще не було
    clock.freeze(datetime(2026, 3, 2, 0, 59, tzinfo=UTC))
    await make_scheduler(ctx).tick()
    assert len([c for c in tg.calls if isinstance(c, SendDocument)]) == 1

    await set_paused(ctx, True)  # бекап працює і на паузі
    clock.freeze(datetime(2026, 3, 2, 1, 0, tzinfo=UTC))  # 03:00 за Києвом
    restarted = open_ctx(settings)
    await make_scheduler(restarted).tick()
    await make_scheduler(restarted).tick()
    docs = [c for c in fake(restarted).calls if isinstance(c, SendDocument)]
    assert len(docs) == 1
    assert docs[0].chat_id == -100777 and docs[0].caption and "учасників: 0" in docs[0].caption
    async with restarted.read() as s:
        assert await get_time(s, LAST_BACKUP_AT) == clock.now()
    # Процес лежав на момент 03:00 наступного дня → бекап при старті, а не пропуск.
    clock.freeze(datetime(2026, 3, 3, 9, 0, tzinfo=UTC))
    third = open_ctx(settings)
    await make_scheduler(third).tick()
    assert len([c for c in fake(third).calls if isinstance(c, SendDocument)]) == 1
    assert len(await events(third, "backup_sent")) == 3
    for c in (ctx, restarted, third):
        await close_ctx(c)


async def test_backup_file_is_valid_sqlite(tmp_path: Path) -> None:
    import sqlite3

    from app.domain.backup import backup_bytes

    ctx = open_ctx(make_settings(tmp_path))
    harness = BotHarness(ctx)
    await activate(harness, CHAT, "BACKUP")
    data = await backup_bytes(ctx.settings.database_path)
    copy = tmp_path / "copy.db"
    copy.write_bytes(data)
    conn = sqlite3.connect(copy)
    assert conn.execute("SELECT code, status FROM participants").fetchall() == [("BACKUP", "active")]
    conn.close()
    await close_ctx(ctx)


async def test_digest_has_only_aggregates(tmp_path: Path) -> None:
    ctx = open_ctx(make_settings(tmp_path, admin_chat_id=-100555))
    harness, tg = BotHarness(ctx), fake(ctx)
    for i in range(4):
        await activate(harness, 4100 + i, f"DIG{i:03d}")
    scheduler = make_scheduler(ctx)
    clock.freeze(DAY1)
    await scheduler.tick()
    clock.freeze(datetime(2026, 3, 4, 7, 0, tzinfo=UTC))  # 04.03 09:00 за Києвом
    await scheduler.tick()
    await scheduler.tick()
    digests = tg.texts(-100555)
    assert len(digests) == 2  # перший запуск (02.03) і 04.03
    text = digests[-1]
    assert "за 03.03.2026" in text
    assert "Активні: 4" in text and "інструкцій надіслано 4" in text and "пропущено 0" in text
    assert "група" not in text.lower() and " A" not in text and " B" not in text
    await close_ctx(ctx)


async def test_failing_job_does_not_stop_others(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    pid = await activate(harness, CHAT, "ISOLA1")
    scheduler = make_scheduler(ctx)

    async def broken() -> None:
        raise ValueError("boom")

    scheduler.schedule[0].run = broken  # close_expired
    clock.freeze(DAY1)
    await scheduler.tick()
    assert tg.texts(CHAT)[-1] == await instruction_for(ctx, pid, 1)
    failed = await events(ctx, "send_failed")
    assert [e.payload["job"] for e in failed] == ["close_expired"]
    assert scheduler.ctx.scheduler_last_tick is not None


async def test_stop_waits_for_current_tick(ctx: AppContext) -> None:
    scheduler = make_scheduler(ctx)
    started, finished = asyncio.Event(), asyncio.Event()

    async def slow() -> None:
        started.set()
        await asyncio.sleep(0.05)
        finished.set()

    scheduler.schedule[0].run = slow
    task = asyncio.create_task(scheduler.run())
    await started.wait()
    scheduler.stop()
    await asyncio.wait_for(task, 2)
    assert finished.is_set()
