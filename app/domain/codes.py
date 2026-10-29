"""Завантаження кодів для CODE_VALIDATION=whitelist."""

import csv
import io
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import AllowedCode, Participant
from app.domain.participants import normalize_code


@dataclass
class UploadReport:
    added: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)


HEADER_NAMES = {"code", "codes", "код", "коди"}


def parse_codes(text: str) -> list[str]:
    """Один код у рядку; для CSV береться перша колонка. Порожні рядки і рядок
    заголовка (code/код) ігноруються."""
    values = []
    for index, row in enumerate(csv.reader(io.StringIO(text))):
        if not row or not row[0].strip():
            continue
        if index == 0 and row[0].strip().lower() in HEADER_NAMES:
            continue
        values.append(row[0].strip())
    return values


async def upload_codes(session: AsyncSession, settings: Settings, raw: list[str], uploaded_by: str) -> UploadReport:
    report = UploadReport()
    existing = set((await session.execute(select(AllowedCode.code))).scalars())
    for value in raw:
        code = normalize_code(value)
        if not settings.code_pattern.match(code):
            report.invalid.append(value)
        elif code in existing:
            report.duplicates.append(code)
        else:
            existing.add(code)
            session.add(AllowedCode(code=code, uploaded_by=uploaded_by))
            report.added.append(code)
    return report


async def code_stats(session: AsyncSession) -> tuple[int, int]:
    """(усього, використано)."""
    total = (await session.execute(select(func.count()).select_from(AllowedCode))).scalar_one()
    used = (
        await session.execute(
            select(func.count()).select_from(AllowedCode).join(Participant, Participant.code == AllowedCode.code)
        )
    ).scalar_one()
    return total, used
