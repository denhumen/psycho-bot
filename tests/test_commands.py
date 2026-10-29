from datetime import UTC, datetime, timedelta

from aiogram.types import ChatMemberBanned, ChatMemberMember, ChatMemberUpdated, Update, User
from sqlalchemy import select

from app.clock import clock
from app.context import AppContext
from app.db.models import Event, Message, Participant, Status, StudySession
from tests.conftest import activate, get_session, mark_sent
from tests.fakes import BotHarness, FakeTelegram

CHAT = 9001


async def participant(ctx: AppContext, pid: int) -> Participant:
    async with ctx.read() as s:
        p = await s.get(Participant, pid)
    assert p is not None
    return p


async def test_status_before_first_instruction(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    await activate(harness, CHAT, "STAT01")
    tg.clear()
    await harness.text(CHAT, "/status")
    assert tg.texts(CHAT) == [ctx.content.t("status_waiting", date="03.03.2026", time="20:00")]


async def test_status_with_pending_checkin_offers_button(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    pid = await activate(harness, CHAT, "STAT02")
    await mark_sent(ctx, pid, 1)
    await harness.press(CHAT, f"ci:{(await get_session(ctx, pid, 1)).id}:did_write:yes")
    await harness.press(CHAT, f"ci:{(await get_session(ctx, pid, 1)).id}:mood:4")
    s2 = await mark_sent(ctx, pid, 2)
    tg.clear()
    await harness.text(CHAT, "/status")
    content = ctx.content
    expected = content.t("status_active", day=2, today=content.t("status_today_pending"), time="20:00")
    assert tg.texts(CHAT) == [expected]
    assert tg.last_markup(CHAT).inline_keyboard[0][0].callback_data == f"cs:{s2.id}"


async def test_time_change_reschedules_only_unsent(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    pid = await activate(harness, CHAT, "TIME02")
    s1_before = await mark_sent(ctx, pid, 1)
    s1_before = await get_session(ctx, pid, 1)
    tg.clear()
    await harness.text(CHAT, "/time")
    assert tg.texts(CHAT) == [ctx.content.t("time_choose_new")]
    await harness.press(CHAT, "time:09:00")
    assert tg.texts(CHAT)[-1] == ctx.content.t("time_changed", time="09:00")

    s1_after = await get_session(ctx, pid, 1)
    assert (s1_after.scheduled_for, s1_after.deadline) == (s1_before.scheduled_for, s1_before.deadline)
    s2 = await get_session(ctx, pid, 2)
    s7 = await get_session(ctx, pid, 7)
    # 04.03 09:00 за Києвом = 07:00 UTC (зимовий час).
    assert s2.scheduled_for == datetime(2026, 3, 4, 7, 0, tzinfo=UTC)
    assert s2.deadline == datetime(2026, 3, 5, 7, 0, tzinfo=UTC)
    assert s7.deadline == s7.scheduled_for + timedelta(hours=24)
    p = await participant(ctx, pid)
    assert p.reminder_time == "09:00"
    async with ctx.read() as s:
        event = (await s.execute(select(Event).where(Event.type == "reminder_time_changed"))).scalar_one()
    assert event.payload == {"old": "20:00", "new": "09:00"}


async def test_time_unavailable_before_consent(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    await harness.text(CHAT, "NOTYET1")
    tg.clear()
    await harness.text(CHAT, "/time")
    assert tg.texts(CHAT) == [ctx.content.t("time_unavailable")]


async def test_stop_flow(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    pid = await activate(harness, CHAT, "STOP01")
    tg.clear()
    await harness.text(CHAT, "/stop")
    assert tg.texts(CHAT) == [ctx.content.t("stop_confirm")]
    await harness.press(CHAT, "stop:no")
    assert (await participant(ctx, pid)).status == Status.ACTIVE
    await harness.press(CHAT, "stop:yes")
    p = await participant(ctx, pid)
    assert p.status == Status.WITHDRAWN and p.withdrawn_at == clock.now()
    assert tg.texts(CHAT)[-2:] == [ctx.content.t("stop_cancelled"), ctx.content.t("stopped")]
    tg.clear()
    await harness.text(CHAT, "/stop")
    assert tg.texts(CHAT) == [ctx.content.t("stop_unavailable")]
    async with ctx.read() as s:
        # Дані учасника не видалено.
        assert len((await s.execute(select(StudySession).where(StudySession.participant_id == pid))).all()) == 7


async def test_help_and_commands_for_unregistered(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    await harness.text(CHAT, "/help")
    await harness.text(CHAT, "/status")
    await harness.text(CHAT, "/stop")
    assert tg.texts(CHAT) == [ctx.content.t("help"), ctx.content.t("not_registered"), ctx.content.t("not_registered")]
    assert "@support_test" in tg.texts(CHAT)[0]


async def test_free_text_saved_and_acked_once_per_6h(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    pid = await activate(harness, CHAT, "TEXT01")
    tg.clear()
    await harness.text(CHAT, "Добрий день, у мене питання")
    await harness.text(CHAT, "І ще одне")
    assert tg.texts(CHAT) == [ctx.content.t("free_text_ack_active")]
    clock.advance(timedelta(hours=6))
    await harness.text(CHAT, "Минуло 6 годин")
    assert len(tg.texts(CHAT)) == 2
    async with ctx.read() as s:
        incoming = (
            await s.execute(select(Message).where(Message.participant_id == pid, Message.direction == "in"))
        ).scalars().all()
    assert [m.text for m in incoming] == ["Добрий день, у мене питання", "І ще одне", "Минуло 6 годин"]
    assert all(m.kind == "participant_text" and m.read_by_admin_at is None for m in incoming)


async def test_free_text_from_not_active(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    await harness.text(CHAT, "TEXT02")
    tg.clear()
    await harness.text(CHAT, "а що далі?")
    assert tg.texts(CHAT) == [ctx.content.t("free_text_ack")]


async def test_media_is_not_stored(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    pid = await activate(harness, CHAT, "MEDIA1")
    tg.clear()
    await harness.photo(CHAT)
    await harness.photo(CHAT)
    assert tg.texts(CHAT) == [ctx.content.t("media_not_supported")]
    async with ctx.read() as s:
        texts = (
            await s.execute(select(Message.text).where(Message.participant_id == pid, Message.direction == "in"))
        ).scalars().all()
    assert texts == ["[медіа: photo]", "[медіа: photo]"]


async def test_block_and_unblock(harness: BotHarness, ctx: AppContext) -> None:
    pid = await activate(harness, CHAT, "BLOCK2")
    user = User(id=CHAT, is_bot=False, first_name="T")
    bot_user = User(id=123456, is_bot=True, first_name="Bot")
    update = Update(
        update_id=777_001,
        my_chat_member=ChatMemberUpdated(
            chat={"id": CHAT, "type": "private"},
            from_user=user,
            date=clock.now(),
            old_chat_member=ChatMemberMember(user=bot_user),
            new_chat_member=ChatMemberBanned(user=bot_user, until_date=0),
        ),
    )
    await harness.feed(update)
    assert (await participant(ctx, pid)).bot_blocked
    await harness.text(CHAT, "/status")  # учасник повернувся і пише
    assert not (await participant(ctx, pid)).bot_blocked
    async with ctx.read() as s:
        types = (await s.execute(select(Event.type).where(Event.type.like("bot_%")))).scalars().all()
    assert types == ["bot_blocked", "bot_unblocked"]
