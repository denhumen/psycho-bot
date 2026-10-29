import asyncio
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.clock import clock
from app.context import AppContext
from app.db.models import AllocationSlot, Event, Participant, Status, StudySession
from app.domain.allocation import allocate, assign_slot, ensure_slots, make_block
from app.domain.sessions import session_times, start_participant
from tests.conftest import add_participant, make_settings, open_ctx

KYIV = ZoneInfo("Europe/Kyiv")


def test_block_is_half_and_half() -> None:
    for _ in range(200):
        assert Counter(make_block(4)) == {"A": 2, "B": 2}
        assert Counter(make_block(6)) == {"A": 3, "B": 3}


async def test_initial_generation(ctx: AppContext) -> None:
    async with ctx.db() as s, s.begin():
        await ensure_slots(s, ctx.settings)
    async with ctx.db() as s, s.begin():
        await ensure_slots(s, ctx.settings)  # повторний старт нічого не додає
    async with ctx.db() as s:
        slots = (await s.execute(select(AllocationSlot).order_by(AllocationSlot.slot_no))).scalars().all()
    assert len(slots) == 100 * 4
    assert [slot.slot_no for slot in slots] == list(range(400))
    for block_no in range(100):
        block = [slot.group for slot in slots if slot.block_no == block_no]
        assert Counter(block) == {"A": 2, "B": 2}


async def test_sequences_are_not_reproducible(tmp_path: Path) -> None:
    sequences = []
    for name in ("one", "two"):
        ctx = open_ctx(make_settings(tmp_path / name))
        async with ctx.db() as s, s.begin():
            await ensure_slots(s, ctx.settings)
        async with ctx.db() as s:
            sequences.append((await s.execute(select(AllocationSlot.group).order_by(AllocationSlot.slot_no))).scalars().all())
        await ctx.engine.dispose()
    assert sequences[0] != sequences[1]


async def test_sequential_balance_and_refill(tmp_path: Path) -> None:
    # 3 початкові блоки, 60 учасників — кілька автоматичних дозаповнень.
    ctx = open_ctx(make_settings(tmp_path, allocation_initial_blocks=3))
    settings = ctx.settings
    counts = Counter[str]()
    for i in range(60):
        pid = await add_participant(ctx.db, f"CODE{i:03d}")
        counts[await allocate(ctx, pid)] += 1
        assert abs(counts["A"] - counts["B"]) <= 2
        if (i + 1) % 4 == 0:
            assert counts["A"] == counts["B"]
    async with ctx.db() as s:
        total = (await s.execute(select(func.count()).select_from(AllocationSlot))).scalar_one()
        free = (
            await s.execute(
                select(func.count()).select_from(AllocationSlot).where(AllocationSlot.participant_id.is_(None))
            )
        ).scalar_one()
    assert total > 3 * 4
    assert free >= settings.allocation_block_size
    await ctx.engine.dispose()


async def test_allocate_is_idempotent(ctx: AppContext) -> None:
    pid = await add_participant(ctx.db, "SAME01")
    first = await allocate(ctx, pid)
    for _ in range(5):
        assert await allocate(ctx, pid) == first
    async with ctx.db() as s:
        taken = (
            await s.execute(select(func.count()).select_from(AllocationSlot).where(AllocationSlot.participant_id == pid))
        ).scalar_one()
    assert taken == 1


async def test_50_concurrent_allocations(ctx: AppContext) -> None:
    pids = [await add_participant(ctx.db, f"CONC{i:03d}") for i in range(50)]
    groups = await asyncio.gather(*(allocate(ctx, pid) for pid in pids))
    async with ctx.db() as s:
        rows = (await s.execute(select(AllocationSlot).where(AllocationSlot.participant_id.is_not(None)))).scalars().all()
    assert len(rows) == 50
    assert len({row.slot_no for row in rows}) == 50
    assert sorted(row.slot_no for row in rows) == list(range(50))  # без пропусків
    assert abs(groups.count("A") - groups.count("B")) <= 2


async def test_50_concurrent_without_process_lock(ctx: AppContext) -> None:
    """Навіть без asyncio.Lock атомарність тримає BEGIN IMMEDIATE на рівні SQLite."""
    async with ctx.db() as s, s.begin():
        await ensure_slots(s, ctx.settings)
    pids = [await add_participant(ctx.db, f"RAW{i:03d}") for i in range(50)]

    async def raw_assign(pid: int) -> str:
        async with ctx.db() as s, s.begin():
            return await assign_slot(s, pid, ctx.settings)

    await asyncio.gather(*(raw_assign(pid) for pid in pids))
    async with ctx.db() as s:
        rows = (await s.execute(select(AllocationSlot).where(AllocationSlot.participant_id.is_not(None)))).scalars().all()
    assert sorted(row.slot_no for row in rows) == list(range(50))


# --- розклад сесій ---------------------------------------------------------


def test_session_times_regular() -> None:
    times = session_times(date(2026, 5, 4), "18:00", KYIV)
    assert len(times) == 7
    # Влітку Київ = UTC+3.
    assert times[0][0] == datetime(2026, 5, 4, 15, 0, tzinfo=UTC)
    assert times[6][0] == datetime(2026, 5, 10, 15, 0, tzinfo=UTC)
    for (scheduled, deadline), (next_scheduled, _) in zip(times, times[1:]):
        assert deadline == next_scheduled
    assert times[6][1] == times[6][0] + timedelta(hours=24)


def test_session_times_across_spring_forward() -> None:
    # 29.03.2026 о 03:00 Київ переходить на UTC+3.
    times = session_times(date(2026, 3, 26), "09:00", KYIV)
    scheduled = [s for s, _ in times]
    assert scheduled[:3] == [datetime(2026, 3, d, 7, 0, tzinfo=UTC) for d in (26, 27, 28)]
    assert scheduled[3:] == [
        datetime(2026, 3, 29, 6, 0, tzinfo=UTC),
        datetime(2026, 3, 30, 6, 0, tzinfo=UTC),
        datetime(2026, 3, 31, 6, 0, tzinfo=UTC),
        datetime(2026, 4, 1, 6, 0, tzinfo=UTC),
    ]
    assert times[2][1] - times[2][0] == timedelta(hours=23)
    for s in scheduled:
        assert s.astimezone(KYIV).hour == 9


def test_session_times_across_fall_back() -> None:
    # 25.10.2026 о 04:00 Київ повертається на UTC+2.
    times = session_times(date(2026, 10, 22), "21:00", KYIV)
    for scheduled, _ in times:
        assert scheduled.astimezone(KYIV).hour == 21
    assert times[2][0] == datetime(2026, 10, 24, 18, 0, tzinfo=UTC)
    assert times[3][0] == datetime(2026, 10, 25, 19, 0, tzinfo=UTC)
    assert times[2][1] - times[2][0] == timedelta(hours=25)


# --- старт учасника --------------------------------------------------------


async def test_start_participant_creates_sessions_once(ctx: AppContext) -> None:
    # 2026-03-02 08:00 UTC = 10:00 у Києві; START_OFFSET_DAYS=1 → старт 03.03.
    pid = await add_participant(ctx.db, "START1", status=Status.CONSENTED, reminder_time="20:00")
    result = await start_participant(ctx, pid)
    assert result is not None and result.started_now
    assert result.first_instruction_at == datetime(2026, 3, 3, 18, 0, tzinfo=UTC)

    again = await start_participant(ctx, pid)
    assert again is not None and not again.started_now
    assert again.first_instruction_at == result.first_instruction_at

    async with ctx.db() as s:
        participant = await s.get(Participant, pid)
        assert participant is not None
        assert participant.status == Status.ACTIVE
        assert participant.start_date == date(2026, 3, 3)
        assert participant.randomized_at == clock.now()
        sessions = (await s.execute(select(StudySession).where(StudySession.participant_id == pid))).scalars().all()
        assert sorted(x.day for x in sessions) == list(range(1, 8))
        events = (await s.execute(select(Event).where(Event.participant_id == pid))).scalars().all()
    randomized = [e for e in events if e.type == "randomized"]
    assert len(randomized) == 1
    assert "group" not in randomized[0].payload
    assert [e.payload for e in events if e.type == "status_changed"] == [{"old": "consented", "new": "active"}]


async def test_start_requires_consent_and_time(ctx: AppContext) -> None:
    registered = await add_participant(ctx.db, "REG001")
    no_time = await add_participant(ctx.db, "NOTIME", status=Status.CONSENTED)
    assert await start_participant(ctx, registered) is None
    assert await start_participant(ctx, no_time) is None
    async with ctx.db() as s:
        taken = (
            await s.execute(select(func.count()).select_from(AllocationSlot).where(AllocationSlot.participant_id.is_not(None)))
        ).scalar_one()
    assert taken == 0


async def test_dev_day_minutes_schedule(tmp_path: Path) -> None:
    ctx = open_ctx(make_settings(tmp_path, dev_day_minutes=5, start_offset_days=0))
    pid = await add_participant(ctx.db, "DEV001", status=Status.CONSENTED, reminder_time="09:00")
    await start_participant(ctx, pid)
    async with ctx.db() as s:
        sessions = (
            await s.execute(select(StudySession).where(StudySession.participant_id == pid).order_by(StudySession.day))
        ).scalars().all()
    now = clock.now()
    assert [x.scheduled_for - now for x in sessions] == [timedelta(minutes=5 * d) for d in range(7)]
    assert sessions[-1].deadline - sessions[-1].scheduled_for == timedelta(minutes=5)
    await ctx.engine.dispose()
