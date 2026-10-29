"""Блокова рандомізація. Послідовність існує тільки в таблиці allocation_slots."""

import secrets

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import clock
from app.config import Settings
from app.context import AppContext
from app.db.models import AllocationSlot

GROUPS = ("A", "B")

_random = secrets.SystemRandom()


def make_block(size: int) -> list[str]:
    block = [GROUPS[0]] * (size // 2) + [GROUPS[1]] * (size // 2)
    _random.shuffle(block)
    return block


async def _append_blocks(session: AsyncSession, count: int, size: int) -> None:
    last_slot, last_block = (
        await session.execute(select(func.max(AllocationSlot.slot_no), func.max(AllocationSlot.block_no)))
    ).one()
    slot_no = -1 if last_slot is None else last_slot
    block_no = -1 if last_block is None else last_block
    for _ in range(count):
        block_no += 1
        for group in make_block(size):
            slot_no += 1
            session.add(AllocationSlot(slot_no=slot_no, block_no=block_no, group=group))
    await session.flush()


async def ensure_slots(session: AsyncSession, settings: Settings) -> None:
    """Перший старт або дозаповнення: якщо вільних слотів менше одного блоку — дописати блоки."""
    free = (
        await session.execute(
            select(func.count()).select_from(AllocationSlot).where(AllocationSlot.participant_id.is_(None))
        )
    ).scalar_one()
    if free < settings.allocation_block_size:
        await _append_blocks(session, settings.allocation_initial_blocks, settings.allocation_block_size)


async def assign_slot(session: AsyncSession, participant_id: int, settings: Settings) -> str:
    """Викликати всередині транзакції (BEGIN IMMEDIATE). Ідемпотентно."""
    existing = (
        await session.execute(select(AllocationSlot).where(AllocationSlot.participant_id == participant_id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing.group
    await ensure_slots(session, settings)
    slot = (
        await session.execute(
            select(AllocationSlot)
            .where(AllocationSlot.participant_id.is_(None))
            .order_by(AllocationSlot.slot_no)
            .limit(1)
        )
    ).scalar_one()
    slot.participant_id = participant_id
    slot.assigned_at = clock.now()
    await session.flush()
    return slot.group


async def allocate(ctx: AppContext, participant_id: int) -> str:
    async with ctx.allocation_lock:
        async with ctx.db() as session, session.begin():
            return await assign_slot(session, participant_id, ctx.settings)


async def group_of(session: AsyncSession, participant_id: int) -> str | None:
    return (
        await session.execute(
            select(AllocationSlot.group).where(AllocationSlot.participant_id == participant_id)
        )
    ).scalar_one_or_none()
