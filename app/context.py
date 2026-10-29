import asyncio
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings


@dataclass
class AppContext:
    """Усе, що потрібно боту, планувальнику й адмінці. Стан учасників — лише в БД."""

    settings: Settings
    engine: AsyncEngine
    db: async_sessionmaker[AsyncSession]
    allocation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # time.monotonic() останнього тіку планувальника; None — планувальник не запущено.
    scheduler_last_tick: float | None = None
    scheduler_enabled: bool = False
