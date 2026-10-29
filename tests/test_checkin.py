from datetime import timedelta

from sqlalchemy import select

from app.clock import clock
from app.context import AppContext
from app.db.models import AllocationSlot, CheckinAnswer, Event, StudySession
from tests.conftest import activate, close_ctx, get_session, mark_sent, open_ctx
from tests.fakes import BotHarness, FakeTelegram, fake

CHAT = 8001


async def answers(ctx: AppContext, session_id: int) -> dict[str, str]:
    async with ctx.read() as s:
        rows = (await s.execute(select(CheckinAnswer).where(CheckinAnswer.session_id == session_id))).scalars()
        return {row.question_id: row.value for row in rows}


async def started_checkin(harness: BotHarness, code: str = "CHECK1") -> tuple[int, StudySession]:
    pid = await activate(harness, CHAT, code)
    clock.advance(timedelta(days=1, hours=10))  # 03.03 20:00 за Києвом — день 1
    study_session = await mark_sent(harness.ctx, pid, 1)
    harness.tg.clear()
    return pid, study_session


async def test_full_checkin(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    pid, s1 = await started_checkin(harness)
    await harness.press(CHAT, f"cs:{s1.id}")
    questions = ctx.content.checkin.questions
    assert tg.texts(CHAT) == [questions[0].text]
    buttons = [b.callback_data for row in tg.last_markup(CHAT).inline_keyboard for b in row]
    assert buttons == [f"ci:{s1.id}:did_write:{v}" for v in ("yes", "partly", "no")]

    await harness.press(CHAT, f"ci:{s1.id}:did_write:partly", message_text=questions[0].text)
    assert tg.texts(CHAT)[-1] == questions[1].text
    scale = tg.last_markup(CHAT).inline_keyboard
    assert [len(row) for row in scale] == [5, 5]

    await harness.press(CHAT, f"ci:{s1.id}:mood:7")
    assert tg.texts(CHAT)[-1] == ctx.content.t("checkin_thanks")
    assert await answers(ctx, s1.id) == {"did_write": "partly", "mood": "7"}
    s1 = await get_session(ctx, pid, 1)
    assert s1.completed_at == clock.now()
    # Обраний варіант дописано в повідомлення, клавіатуру прибрано.
    edits = [c for c in tg.calls if type(c).__name__ == "EditMessageText"]
    assert edits[0].text.endswith("✅ Частково") and edits[0].reply_markup is None


async def test_repeated_press_keeps_first_answer(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    _, s1 = await started_checkin(harness)
    await harness.press(CHAT, f"cs:{s1.id}")
    await harness.press(CHAT, f"ci:{s1.id}:did_write:yes")
    sent_before = len(tg.texts(CHAT))
    await harness.press(CHAT, f"ci:{s1.id}:did_write:no")
    await harness.press(CHAT, f"ci:{s1.id}:did_write:yes")
    assert await answers(ctx, s1.id) == {"did_write": "yes"}
    assert len(tg.texts(CHAT)) == sent_before  # друге питання не надіслано повторно


async def test_press_after_deadline_is_ignored(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    _, s1 = await started_checkin(harness)
    await harness.press(CHAT, f"cs:{s1.id}")
    clock.advance(timedelta(days=1))  # дедлайн = початок дня 2
    await harness.press(CHAT, f"ci:{s1.id}:did_write:yes")
    await harness.press(CHAT, f"cs:{s1.id}")
    assert await answers(ctx, s1.id) == {}
    assert [a.text for a in tg.callback_answers()[-2:]] == [ctx.content.t("checkin_expired")] * 2


async def test_restart_mid_checkin_resumes_from_db(harness: BotHarness, ctx: AppContext) -> None:
    pid, s1 = await started_checkin(harness)
    await harness.press(CHAT, f"cs:{s1.id}")
    # Процес упав якраз під час надсилання другого питання.
    harness.tg.fail_next.append(RuntimeError("killed"))
    try:
        await harness.press(CHAT, f"ci:{s1.id}:did_write:yes")
    except RuntimeError:
        pass

    restarted = open_ctx(ctx.settings)
    bot2, tg2 = BotHarness(restarted), fake(restarted)
    await bot2.press(CHAT, f"cs:{s1.id}")
    second = restarted.content.checkin.questions[1]
    assert tg2.texts(CHAT) == [second.text]  # продовжуємо з першого питання без відповіді
    await bot2.press(CHAT, f"ci:{s1.id}:mood:3")
    assert tg2.texts(CHAT)[-1] == restarted.content.t("checkin_thanks")
    assert await answers(restarted, s1.id) == {"did_write": "yes", "mood": "3"}
    assert (await get_session(restarted, pid, 1)).completed_at is not None
    await close_ctx(restarted)


async def test_completed_session(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    _, s1 = await started_checkin(harness)
    await harness.press(CHAT, f"ci:{s1.id}:did_write:yes")
    await harness.press(CHAT, f"ci:{s1.id}:mood:5")
    tg.clear()
    await harness.press(CHAT, f"cs:{s1.id}")
    await harness.press(CHAT, f"ci:{s1.id}:mood:9")
    assert tg.texts(CHAT) == []
    assert [a.text for a in tg.callback_answers()] == [ctx.content.t("checkin_already_done")] * 2
    async with ctx.read() as s:
        completed = (await s.execute(select(Event).where(Event.type == "checkin_completed"))).scalars().all()
    assert len(completed) == 1 and completed[0].payload == {"day": 1}


async def test_unsent_foreign_or_withdrawn_sessions_are_outdated(
    harness: BotHarness, tg: FakeTelegram, ctx: AppContext
) -> None:
    pid, s1 = await started_checkin(harness)
    s2 = await get_session(ctx, pid, 2)
    await harness.press(CHAT, f"cs:{s2.id}")  # ще не надіслана
    await harness.press(9999, f"cs:{s1.id}")  # чужа сесія
    await harness.press(CHAT, f"ci:{s1.id}:did_write:maybe")  # невідоме значення
    await harness.press(CHAT, "stop:yes")
    await harness.press(CHAT, f"ci:{s1.id}:did_write:yes")  # вибув
    outdated = ctx.content.t("button_outdated")
    texts = [a.text for a in tg.callback_answers()]
    assert [texts[0], texts[1], texts[2], texts[-1]] == [outdated] * 4
    assert await answers(ctx, s1.id) == {}


async def test_checkin_texts_same_for_both_groups(harness: BotHarness, tg: FakeTelegram, ctx: AppContext) -> None:
    """Інваріант 6: питання і подяка не залежать від групи (беруться лише з checkin.json/texts.json)."""
    chats = list(range(8100, 8108))
    sessions = []
    for i, chat in enumerate(chats):
        pid = await activate(harness, chat, f"GRP{i:03d}")
        sessions.append(await mark_sent(ctx, pid, 1))
    clock.advance(timedelta(days=1, hours=10))
    tg.clear()
    for chat, s1 in zip(chats, sessions, strict=True):
        await harness.press(chat, f"cs:{s1.id}")
        await harness.press(chat, f"ci:{s1.id}:did_write:yes")
        await harness.press(chat, f"ci:{s1.id}:mood:5")
    async with ctx.read() as s:
        groups = set((await s.execute(select(AllocationSlot.group).where(AllocationSlot.participant_id.is_not(None)))).scalars())
    assert groups == {"A", "B"}
    assert len({tuple(tg.texts(chat)) for chat in chats}) == 1
