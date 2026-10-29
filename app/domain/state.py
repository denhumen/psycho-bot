"""Таблиця settings (key/value): пауза планувальника і час останніх щоденних задач."""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Setting

SCHEDULER_PAUSED = "scheduler_paused"
LAST_BACKUP_AT = "last_backup_at"
LAST_DIGEST_AT = "last_digest_at"


async def get_value(session: AsyncSession, key: str) -> str | None:
    row = await session.get(Setting, key)
    return row.value if row is not None else None


async def set_value(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value


async def is_paused(session: AsyncSession) -> bool:
    return await get_value(session, SCHEDULER_PAUSED) == "true"


async def get_time(session: AsyncSession, key: str) -> datetime | None:
    value = await get_value(session, key)
    return datetime.fromisoformat(value) if value else None


async def set_time(session: AsyncSession, key: str, moment: datetime) -> None:
    await set_value(session, key, moment.isoformat())
