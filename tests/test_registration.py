from datetime import UTC, datetime
from pathlib import Path

from aiogram.types import Chat, Message, Update, User
from sqlalchemy import func, select

from app.clock import clock
from app.context import AppContext
from app.db.models import AllocationSlot, AllowedCode, Contact, Event, Participant, Status
from tests.conftest import close_ctx, make_settings, open_ctx
from tests.fakes import BotHarness, FakeTelegram, fake

CHAT = 5001
OTHER_CHAT = 5002


async def participant_by_code(ctx: AppContext, code: str) -> Participant | None:
    async with ctx.read() as s:
        return (await s.execute(select(Participant).where(Participant.code == code))).scalar_one_or_none()


async def count(ctx: AppContext, model: type) -> int:
    async with ctx.read() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def test_start_without_code_asks_for_code(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    await harness.text(CHAT, "/start")
    assert tg.texts(CHAT) == [ctx.content.t("welcome_ask_code")]


async def test_code_is_normalized_and_consent_shown(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    await harness.text(CHAT, "  abc-123 ")
    participant = await participant_by_code(ctx, "ABC-123")
    assert participant is not None and participant.status == Status.REGISTERED
    assert tg.texts(CHAT) == [ctx.content.t("consent")]
    buttons = [b.callback_data for row in tg.last_markup(CHAT).inline_keyboard for b in row]
    assert buttons == ["consent:yes", "consent:no"]
    async with ctx.read() as s:
        contact = await s.get(Contact, participant.id)
    assert contact is not None and contact.chat_id == CHAT


async def test_cyrillic_code(harness: BotHarness, ctx: AppContext) -> None:
    await harness.text(CHAT, "код_їжак1")
    assert await participant_by_code(ctx, "КОД_ЇЖАК1") is not None


async def test_invalid_format(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    await harness.text(CHAT, "ab")
    await harness.text(CHAT, "з пробілом")
    assert tg.texts(CHAT) == [ctx.content.t("code_invalid_format")] * 2
    assert await count(ctx, Participant) == 0


async def test_deep_link(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    await harness.text(CHAT, "/start DEEP42")
    assert await participant_by_code(ctx, "DEEP42") is not None
    assert tg.texts(CHAT) == [ctx.content.t("consent")]


async def test_whitelist(tmp_path: Path) -> None:
    ctx = open_ctx(make_settings(tmp_path, code_validation="whitelist"))
    harness, tg = BotHarness(ctx), fake(ctx)
    async with ctx.db() as s, s.begin():
        s.add(AllowedCode(code="ALLOWED1"))
    await harness.text(CHAT, "UNKNOWN1")
    assert tg.texts(CHAT) == [ctx.content.t("code_not_found")]
    assert "@support_test" in tg.texts(CHAT)[0]
    await harness.text(CHAT, "allowed1")
    assert await participant_by_code(ctx, "ALLOWED1") is not None
    await close_ctx(ctx)


async def test_code_conflict(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    await harness.text(CHAT, "TAKEN1")
    await harness.text(OTHER_CHAT, "taken1")
    assert tg.texts(OTHER_CHAT) == [ctx.content.t("code_conflict")]
    assert await count(ctx, Participant) == 1
    async with ctx.read() as s:
        events = (await s.execute(select(Event).where(Event.type == "code_conflict"))).scalars().all()
        owner = await participant_by_code(ctx, "TAKEN1")
    assert len(events) == 1
    assert owner is not None and events[0].participant_id == owner.id
    assert events[0].payload == {"code": "TAKEN1"}


async def test_repeated_start_shows_current_step(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    await harness.text(CHAT, "REPEAT1")
    tg.clear()
    await harness.text(CHAT, "/start")
    await harness.text(CHAT, "/start OTHERCODE")
    # Двічі показано поточний крок (згода), нового учасника не створено.
    assert tg.texts(CHAT) == [ctx.content.t("consent")] * 2
    assert await count(ctx, Participant) == 1


async def register_until_ready(harness: BotHarness, chat_id: int, code: str) -> None:
    await harness.text(chat_id, code)
    await harness.press(chat_id, "consent:yes")
    await harness.press(chat_id, "time:20:00")


async def test_full_registration_flow(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    await register_until_ready(harness, CHAT, "FLOW01")
    content = ctx.content
    assert tg.texts(CHAT) == [
        content.t("consent"),
        content.t("choose_time"),
        content.t("time_explanation", time="20:00"),
    ]
    participant = await participant_by_code(ctx, "FLOW01")
    assert participant is not None and participant.status == Status.CONSENTED
    assert participant.reminder_time == "20:00"
    async with ctx.read() as s:
        assigned = (
            await s.execute(select(func.count()).select_from(AllocationSlot).where(AllocationSlot.participant_id.is_not(None)))
        ).scalar_one()
    assert assigned == 0  # без «Готовий почати» групи немає

    await harness.press(CHAT, "ready")
    # 02.03 10:00 за Києвом, START_OFFSET_DAYS=1 → 03.03 о 20:00.
    assert tg.texts(CHAT)[-1] == content.t("started", date="03.03.2026", time="20:00")
    participant = await participant_by_code(ctx, "FLOW01")
    assert participant is not None and participant.status == Status.ACTIVE
    # Кожна кнопка отримала answer_callback_query, повідомлення з кнопками відредаговано.
    assert len(tg.callback_answers()) == 3


async def test_decline(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    await harness.text(CHAT, "NOPE01")
    await harness.press(CHAT, "consent:no")
    participant = await participant_by_code(ctx, "NOPE01")
    assert participant is not None and participant.status == Status.DECLINED
    assert tg.texts(CHAT)[-1] == ctx.content.t("consent_declined")
    tg.clear()
    await harness.press(CHAT, "consent:yes")  # стара кнопка
    assert tg.texts(CHAT) == []
    assert tg.callback_answers()[-1].text == ctx.content.t("button_outdated")
    await harness.text(CHAT, "/start")
    assert tg.texts(CHAT) == [ctx.content.t("status_declined")]


async def test_double_presses_do_not_duplicate(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    await harness.text(CHAT, "TWICE1")
    await harness.press(CHAT, "consent:yes")
    await harness.press(CHAT, "consent:yes")
    await harness.press(CHAT, "time:20:00")
    await harness.press(CHAT, "time:20:00")
    await harness.press(CHAT, "ready")
    await harness.press(CHAT, "ready")
    content = ctx.content
    assert tg.texts(CHAT) == [
        content.t("consent"),
        content.t("choose_time"),
        content.t("time_explanation", time="20:00"),
        content.t("started", date="03.03.2026", time="20:00"),
    ]
    async with ctx.read() as s:
        assigned = (
            await s.execute(select(func.count()).select_from(AllocationSlot).where(AllocationSlot.participant_id.is_not(None)))
        ).scalar_one()
        statuses = (await s.execute(select(Event.payload).where(Event.type == "status_changed"))).scalars().all()
    assert assigned == 1
    assert statuses == [{"old": "registered", "new": "consented"}, {"old": "consented", "new": "active"}]


async def test_unknown_time_option_is_rejected(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    await harness.text(CHAT, "TIME01")
    await harness.press(CHAT, "consent:yes")
    await harness.press(CHAT, "time:04:00")
    participant = await participant_by_code(ctx, "TIME01")
    assert participant is not None and participant.reminder_time is None
    assert tg.callback_answers()[-1].text == ctx.content.t("button_outdated")


async def test_redelivered_update_is_ignored(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    update = Update(
        update_id=999_001,
        message=Message(
            message_id=1,
            date=clock.now(),
            chat=Chat(id=CHAT, type="private"),
            from_user=User(id=CHAT, is_bot=False, first_name="T"),
            text="REDELIVER",
        ),
    )
    await harness.feed(update)
    # «Рестарт»: новий процес на тій самій БД, Telegram повертає той самий апдейт.
    restarted = open_ctx(ctx.settings)
    await BotHarness(restarted).feed(update)
    assert fake(restarted).texts() == []
    assert tg.texts(CHAT) == [ctx.content.t("consent")]
    assert await count(ctx, Participant) == 1
    await close_ctx(restarted)


async def test_update_interrupted_mid_handler_is_processed_again(
    harness: BotHarness, tg: FakeTelegram, ctx: AppContext
) -> None:
    await harness.text(CHAT, "CRASH1")
    update = Update(
        update_id=999_002,
        callback_query={
            "id": "cb",
            "from": {"id": CHAT, "is_bot": False, "first_name": "T"},
            "chat_instance": "x",
            "data": "consent:yes",
            "message": {"message_id": 1, "date": int(clock.now().timestamp()), "chat": {"id": CHAT, "type": "private"}, "text": "…"},
        },
    )
    # Процес «помер» під час надсилання відповіді: виняток посеред хендлера.
    tg.fail_next.append(RuntimeError("process killed"))
    try:
        await harness.feed(update)
    except RuntimeError:
        pass
    restarted = open_ctx(ctx.settings)
    tg2 = fake(restarted)
    await BotHarness(restarted).feed(update)
    # Згода вже зафіксована до падіння — повтор не дублює її, кнопка відповідає «неактуальна».
    participant = await participant_by_code(restarted, "CRASH1")
    assert participant is not None and participant.status == Status.CONSENTED
    assert tg2.texts(CHAT) == []
    # Але учасник не застрягає: /start показує наступний крок (вибір часу).
    await BotHarness(restarted).text(CHAT, "/start")
    assert tg2.texts(CHAT) == [restarted.content.t("choose_time")]
    await close_ctx(restarted)


async def test_group_chats_are_ignored(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    await harness.text(-100500, "GROUPCODE", chat_type="supergroup")
    assert tg.calls == []
    assert await count(ctx, Participant) == 0


async def test_media_from_unregistered(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    await harness.photo(CHAT)
    assert tg.texts(CHAT) == [ctx.content.t("not_registered")]


async def test_started_message_is_same_for_both_groups(tmp_path: Path) -> None:
    """Інваріант 6: тексти реєстрації не залежать від групи."""
    ctx = open_ctx(make_settings(tmp_path))
    harness, tg = BotHarness(ctx), fake(ctx)
    for i in range(8):
        await register_until_ready(harness, 6000 + i, f"SAME{i:02d}")
        await harness.press(6000 + i, "ready")
    async with ctx.read() as s:
        groups = set((await s.execute(select(AllocationSlot.group).where(AllocationSlot.participant_id.is_not(None)))).scalars())
    assert groups == {"A", "B"}
    sequences = {tuple(tg.texts(6000 + i)) for i in range(8)}
    assert len(sequences) == 1
    await close_ctx(ctx)


def test_clock_is_frozen() -> None:
    assert clock.now() == datetime(2026, 3, 2, 8, 0, tzinfo=UTC)
