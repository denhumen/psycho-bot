"""Копія БД через sqlite3 backup API (живий файл напряму не копіюємо)."""

import asyncio
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select

from app.clock import clock
from app.context import AppContext
from app.db.models import Participant


def backup_to(db_path: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(db_path)
    destination = sqlite3.connect(target)
    try:
        with destination:
            source.backup(destination)
    finally:
        destination.close()
        source.close()


async def backup_bytes(db_path: Path) -> bytes:
    with tempfile.TemporaryDirectory() as folder:
        target = Path(folder) / "backup.db"
        await asyncio.to_thread(backup_to, db_path, target)
        return target.read_bytes()


def backup_filename(moment: datetime, ctx: AppContext) -> str:
    return f"study-{moment.astimezone(ctx.settings.tz):%Y-%m-%d-%H%M}.db"


async def send_backup(ctx: AppContext, chat_id: int) -> None:
    """Надсилає копію БД документом. Підпис — службовий (для owner), не для учасників."""
    now = clock.now()
    async with ctx.read() as session:
        participants = (await session.execute(select(func.count()).select_from(Participant))).scalar_one()
    data = await backup_bytes(ctx.settings.database_path)
    caption = f"Бекап БД {now.astimezone(ctx.settings.tz):%d.%m.%Y %H:%M}, учасників: {participants}"
    await ctx.sender.send_document(chat_id, backup_filename(now, ctx), data, caption)
