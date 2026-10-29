from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings

if TYPE_CHECKING:
    from app.bot.sender import Sender
    from app.domain.content import Content


@dataclass
class AppContext:
    """Усе, що потрібно боту, планувальнику й адмінці. Стан учасників — лише в БД."""

    settings: Settings
    engine: AsyncEngine
    db: async_sessionmaker[AsyncSession]  # запис (BEGIN IMMEDIATE)
    read: async_sessionmaker[AsyncSession]  # лише читання
    content: Content
    sender: Sender
    allocation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # time.monotonic() останнього тіку планувальника; None — планувальник не запущено.
    scheduler_last_tick: float | None = None
    scheduler_enabled: bool = False
