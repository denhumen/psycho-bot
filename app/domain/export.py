"""Експорт даних у CSV. Єдине місце, де група учасника покидає БД (лише для owner/CLI)."""

import csv
import io
import json
import zipfile
from collections import defaultdict
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import AdminUser, AllocationSlot, CheckinAnswer, Event, Kind, Message, Participant, StudySession
from app.domain.content import Content


def _cell(value: Any, settings: Settings) -> Any:
    # Часи — ISO 8601 за Києвом зі зміщенням: однозначно і зручно для аналізу.
    if isinstance(value, datetime):
        return value.astimezone(settings.tz).isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return int(value)
    return "" if value is None else value


def _csv(header: list[str], rows: list[list[Any]], settings: Settings) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        writer.writerow([_cell(value, settings) for value in row])
    # BOM — щоб Excel коректно відкрив кирилицю.
    return ("﻿" + buffer.getvalue()).encode("utf-8")


async def build_export(session: AsyncSession, settings: Settings, content: Content) -> dict[str, bytes]:
    participants = (await session.execute(select(Participant).order_by(Participant.id))).scalars().all()
    codes = {p.id: p.code for p in participants}
    groups = dict(
        (await session.execute(select(AllocationSlot.participant_id, AllocationSlot.group).where(
            AllocationSlot.participant_id.is_not(None)
        ))).tuples().all()
    )
    sessions = (
        await session.execute(select(StudySession).order_by(StudySession.participant_id, StudySession.day))
    ).scalars().all()
    session_index = {s.id: s for s in sessions}
    answers = (await session.execute(select(CheckinAnswer).order_by(CheckinAnswer.id))).scalars().all()
    messages = (await session.execute(select(Message).order_by(Message.id))).scalars().all()
    admins = dict((await session.execute(select(AdminUser.id, AdminUser.username))).tuples().all())
    events = (await session.execute(select(Event).order_by(Event.id))).scalars().all()

    files: dict[str, bytes] = {}
    files["participants.csv"] = _csv(
        [
            "code", "group", "status", "reminder_time", "start_date", "created_at", "consented_at",
            "randomized_at", "completed_at", "withdrawn_at", "post_survey_sent_at", "last_activity_at", "bot_blocked",
        ],
        [
            [
                p.code, groups.get(p.id), p.status, p.reminder_time, p.start_date, p.created_at, p.consented_at,
                p.randomized_at, p.completed_at, p.withdrawn_at, p.post_survey_sent_at, p.last_activity_at,
                p.bot_blocked,
            ]
            for p in participants
        ],
        settings,
    )
    files["sessions.csv"] = _csv(
        ["code", "day", "scheduled_for", "deadline", "sent_at", "reminder_sent_at", "completed_at", "missed_at"],
        [
            [codes[s.participant_id], s.day, s.scheduled_for, s.deadline, s.sent_at, s.reminder_sent_at,
             s.completed_at, s.missed_at]
            for s in sessions
        ],
        settings,
    )
    files["checkin_answers.csv"] = _csv(
        ["code", "day", "question_id", "value", "answered_at"],
        [
            [codes[session_index[a.session_id].participant_id], session_index[a.session_id].day, a.question_id,
             a.value, a.answered_at]
            for a in answers
        ],
        settings,
    )
    # Широкий формат: один рядок = учасник × день, колонки = питання.
    question_ids = [q.id for q in content.checkin.questions]
    known = set(question_ids)
    extra = sorted({a.question_id for a in answers} - known)  # питання, які вже прибрали з контенту
    by_session: dict[int, dict[str, str]] = defaultdict(dict)
    for a in answers:
        by_session[a.session_id][a.question_id] = a.value
    files["checkin_answers_wide.csv"] = _csv(
        ["code", "day", "completed_at", "missed_at", *question_ids, *extra],
        [
            [codes[s.participant_id], s.day, s.completed_at, s.missed_at,
             *[by_session[s.id].get(q) for q in [*question_ids, *extra]]]
            for s in sessions
        ],
        settings,
    )
    files["messages.csv"] = _csv(
        ["id", "code", "direction", "kind", "text_key", "sender_admin", "session_day", "text", "created_at",
         "read_by_admin_at"],
        [
            [
                m.id, codes[m.participant_id], m.direction, m.kind, m.text_key,
                admins.get(m.sender_admin_id) if m.sender_admin_id else None, m.session_day,
                # Текст інструкції не експортуємо: за ним можна визначити групу.
                None if m.kind == Kind.INSTRUCTION else m.text,
                m.created_at, m.read_by_admin_at,
            ]
            for m in messages
        ],
        settings,
    )
    files["events.csv"] = _csv(
        ["id", "at", "actor", "code", "type", "payload"],
        [
            [e.id, e.at, e.actor, codes.get(e.participant_id) if e.participant_id else None, e.type,
             json.dumps(e.payload, ensure_ascii=False)]
            for e in events
        ],
        settings,
    )
    return files


def zip_files(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()
