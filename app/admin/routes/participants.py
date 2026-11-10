from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.auth import current_admin, get_ctx, require_owner
from app.admin.overview import NOT_SENT, WAITING, apply_filters, cell, load_rows
from app.admin.web import STATUS_LABELS, flash, redirect, render, safe_next
from app.clock import clock
from app.context import AppContext
from app.db.models import (
    AdminUser,
    CheckinAnswer,
    Event,
    Kind,
    Message,
    Participant,
    ReminderTemplate,
    Status,
    StudySession,
)
from app.domain.content import plain_text
from app.domain.events import admin_actor, log_event, set_status
from app.domain.participants import chat_of, normalize_code
from app.domain.sessions import DAYS

router = APIRouter()

PAGE_SIZE = 50
NO_MESSAGES = (Status.WITHDRAWN, Status.DECLINED)
WITHDRAWABLE = (Status.REGISTERED, Status.CONSENTED, Status.ACTIVE)
FILTERS = {
    "": "Усі",
    "unread": "Чекають на відповідь",
    "not_done_today": "Не пройшли сьогодні",
    "missed_streak": "Пропуски 2+ дні",
    "not_started": "Не стартували",
    "blocked": "Заблокували бота",
}


def list_query(status: str, flag: str, q: str, **extra: Any) -> str:
    params = {k: v for k, v in {"status": status, "flag": flag, "q": q, **extra}.items() if v not in ("", None, 1)}
    return "?" + urlencode(params) if params else ""


@router.get("/participants")
async def participants_list(
    request: Request,
    status: str = "",
    flag: str = "",
    q: str = "",
    page: int = 1,
    peek: str = "",
    admin: AdminUser = Depends(current_admin),
) -> Response:
    ctx = get_ctx(request)
    async with ctx.read() as session:
        all_rows = await load_rows(session, clock.now())
    rows = apply_filters(all_rows, status=status, flag=flag, q=q)
    pages = max(1, -(-len(rows) // PAGE_SIZE))
    page = min(max(page, 1), pages)
    start = (page - 1) * PAGE_SIZE
    # Відкритий peek після надсилання повідомлення з нього (працює і без JS).
    peek_card = await load_card(ctx, peek, admin) if peek else None
    return await render(
        request,
        "participants.html",
        admin,
        rows=rows[start : start + PAGE_SIZE],
        found=len(rows),
        first=start + 1 if rows else 0,
        last=min(start + PAGE_SIZE, len(rows)),
        page=page,
        pages=pages,
        statuses=STATUS_LABELS,
        filters=FILTERS,
        status=status,
        flag=flag,
        q=q,
        query=lambda **extra: list_query(**{"status": status, "flag": flag, "q": q, **extra}),
        card=peek_card,
        list_url="/participants" + list_query(status, flag, q, page=page),
    )


# --- картка і peek ------------------------------------------------------------


@dataclass
class DayBlock:
    session: StudySession
    state: str
    answers: list[tuple[str, str]]

    @property
    def css(self) -> str:
        return {WAITING: "today", NOT_SENT: "future"}.get(self.state, "")


@dataclass
class ChatItem:
    at: datetime
    kind: str  # "msg" | "sys"
    text: str
    direction: str = ""
    author: str = ""
    unread: bool = False
    icon: str = ""


@dataclass
class Card:
    participant: Participant
    days: list[DayBlock]
    current_day: int
    chat: list[ChatItem]
    unread: int
    events: list[Event]
    templates: list[ReminderTemplate]
    can_message: bool
    can_withdraw: bool


def build_chat(
    ctx: AppContext, messages: list[Message], sessions: list[StudySession], authors: dict[int, str]
) -> list[ChatItem]:
    tz = ctx.settings.tz
    items: list[ChatItem] = []
    for m in messages:
        hhmm = m.created_at.astimezone(tz).strftime("%H:%M")
        if m.kind == Kind.INSTRUCTION:
            # Інваріант 8: текст інструкції в адмінці не показується.
            items.append(ChatItem(m.created_at, "sys", f"Інструкція, день {m.session_day}, {hhmm}", icon="doc"))
        elif m.kind == Kind.AUTO_REMINDER:
            items.append(ChatItem(m.created_at, "sys", f"Авто-нагадування, {hhmm}"))
        elif m.kind == Kind.CHECKIN or m.text_key == "checkin_thanks":
            continue  # питання опитування показуються одним рядком «Опитування пройдено»
        elif m.direction == "in":
            unread = m.read_by_admin_at is None
            items.append(ChatItem(m.created_at, "msg", m.text, direction="in", author="Учасник", unread=unread))
        else:
            author = authors.get(m.sender_admin_id or 0, "") if m.kind == Kind.MANUAL else "Бот"
            # Розширені тексти з texts.json надсилаються з HTML-розміткою — в адмінці без тегів.
            text = plain_text(m.text) if ctx.content.rich(m.text_key) else m.text
            items.append(ChatItem(m.created_at, "msg", text, direction="out", author=author))
    for s in sessions:
        if s.completed_at is not None:
            hhmm = s.completed_at.astimezone(tz).strftime("%H:%M")
            items.append(ChatItem(s.completed_at, "sys", f"Опитування дня {s.day} пройдено, {hhmm}"))
    items.sort(key=lambda item: item.at)
    return items


async def _participant_or_404(session: AsyncSession, code: str) -> Participant:
    participant = (
        await session.execute(select(Participant).where(Participant.code == normalize_code(code)))
    ).scalar_one_or_none()
    if participant is None:
        raise HTTPException(status_code=404, detail="Учасника не знайдено")
    return participant


async def load_card(ctx: AppContext, code: str, admin: AdminUser) -> Card:
    """Дані картки і peek. Відкриття позначає вхідні прочитаними. Групу не завантажуємо."""
    content = ctx.content
    now = clock.now()
    async with ctx.read() as session:
        participant = await _participant_or_404(session, code)
        sessions = list(
            (
                await session.execute(
                    select(StudySession)
                    .where(StudySession.participant_id == participant.id)
                    .order_by(StudySession.day)
                )
            ).scalars()
        )
        answers = (
            await session.execute(select(CheckinAnswer).where(CheckinAnswer.session_id.in_([s.id for s in sessions])))
        ).scalars().all()
        messages = list(
            (
                await session.execute(
                    select(Message)
                    .where(Message.participant_id == participant.id)
                    .order_by(Message.created_at, Message.id)
                )
            ).scalars()
        )
        authors = dict((await session.execute(select(AdminUser.id, AdminUser.username))).tuples().all())
        events = list(
            (
                await session.execute(
                    select(Event).where(Event.participant_id == participant.id).order_by(Event.at.desc()).limit(30)
                )
            ).scalars()
        )
        templates = list(
            (
                await session.execute(
                    select(ReminderTemplate)
                    .where(ReminderTemplate.is_active.is_(True))
                    .order_by(ReminderTemplate.title)
                )
            ).scalars()
        )

    by_session: dict[int, list[tuple[str, str]]] = {}
    order = {q.id: i for i, q in enumerate(content.checkin.questions)}
    for item in sorted(answers, key=lambda a: order.get(a.question_id, 99)):
        question = content.checkin.by_id(item.question_id)
        label = (question.label or question.id) if question else item.question_id
        value = dict(question.choices()).get(item.value, item.value) if question else item.value
        by_session.setdefault(item.session_id, []).append((label, value))
    days = [DayBlock(s, cell(s, now), by_session.get(s.id, [])) for s in sessions]
    sent_days = [s.day for s in sessions if s.sent_at is not None]
    chat = build_chat(ctx, messages, sessions, authors)
    unread = sum(1 for item in chat if item.unread)

    if unread:
        async with ctx.db() as session, session.begin():
            await session.execute(
                update(Message)
                .where(
                    Message.participant_id == participant.id,
                    Message.direction == "in",
                    Message.read_by_admin_at.is_(None),
                )
                .values(read_by_admin_at=now)
            )

    return Card(
        participant=participant,
        days=days,
        current_day=max(sent_days, default=0),
        chat=chat,
        unread=unread,
        events=events,
        templates=templates,
        can_message=participant.status not in NO_MESSAGES,
        can_withdraw=admin.role == "owner" and participant.status in WITHDRAWABLE,
    )


@router.get("/participants/{code}")
async def participant_card(request: Request, code: str, admin: AdminUser = Depends(current_admin)) -> Response:
    card = await load_card(get_ctx(request), code, admin)
    return await render(request, "participant.html", admin, card=card, now=clock.now(), days_total=DAYS)


@router.get("/participants/{code}/peek")
async def participant_peek(
    request: Request, code: str, back: str = "", admin: AdminUser = Depends(current_admin)
) -> Response:
    """HTML-фрагмент бічної панелі для списку учасників."""
    card = await load_card(get_ctx(request), code, admin)
    list_url = safe_next(back, "/participants")
    return await render(request, "_peek.html", admin, card=card, list_url=list_url, now=clock.now())


@router.post("/participants/{code}/message")
async def send_manual_message(
    request: Request,
    code: str,
    text: str = Form(""),
    next: str = Form(""),
    admin: AdminUser = Depends(current_admin),
) -> Response:
    ctx = get_ctx(request)
    text = text.strip()
    async with ctx.read() as session:
        participant = await _participant_or_404(session, code)
        chat_id = await chat_of(session, participant.id)
    url = safe_next(next, f"/participants/{participant.code}")
    if not text:
        flash(request, "Повідомлення порожнє.", "error")
        return redirect(url)
    if participant.status in NO_MESSAGES:
        flash(request, "Учасник вибув або не погодився, писати йому не можна.", "error")
        return redirect(url)
    if chat_id is None:
        flash(request, "Контакт учасника видалено, надіслати неможливо.", "error")
        return redirect(url)

    async def audit(session: AsyncSession) -> None:
        log_event(session, "admin_message_sent", actor=admin_actor(admin.id), participant_id=participant.id)

    result = await ctx.sender.send(
        chat_id, text, participant_id=participant.id, kind=Kind.MANUAL, sender_admin_id=admin.id, after=audit
    )
    if result.ok:
        flash(request, "Повідомлення надіслано.")
    elif result.blocked:
        flash(request, "Учасник заблокував бота, повідомлення не доставлено.", "error")
    else:
        flash(request, "Не вдалося надіслати повідомлення. Спробуйте пізніше.", "error")
    return redirect(url)


@router.post("/participants/{code}/withdraw")
async def mark_withdrawn(request: Request, code: str, admin: AdminUser = Depends(require_owner)) -> Response:
    ctx = get_ctx(request)
    async with ctx.db() as session, session.begin():
        participant = await _participant_or_404(session, code)
        if participant.status in WITHDRAWABLE:
            set_status(session, participant, Status.WITHDRAWN, actor=admin_actor(admin.id))
            flash(request, "Учасника позначено як вибулого.")
    return redirect(f"/participants/{participant.code}")
