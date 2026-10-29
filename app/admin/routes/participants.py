from dataclasses import dataclass

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.auth import current_admin, get_ctx, require_owner
from app.admin.overview import CELL_TITLES, apply_filters, load_rows
from app.admin.web import STATUS_LABELS, flash, redirect, render
from app.clock import clock
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
from app.domain.events import admin_actor, log_event, set_status
from app.domain.participants import chat_of, normalize_code
from app.domain.sessions import DAYS

router = APIRouter()

NO_MESSAGES = (Status.WITHDRAWN, Status.DECLINED)
WITHDRAWABLE = (Status.REGISTERED, Status.CONSENTED, Status.ACTIVE)
FILTERS = {
    "": "усі",
    "not_done_today": "не пройшли сьогодні",
    "missed_streak": "пропустили 2+ дні поспіль",
    "unread": "є непрочитані",
    "not_started": "зареєструвались, але не стартували",
    "blocked": "заблокували бота",
}


@router.get("/participants")
async def participants_list(
    request: Request,
    status: str = "",
    flag: str = "",
    q: str = "",
    admin: AdminUser = Depends(current_admin),
) -> Response:
    ctx = get_ctx(request)
    async with ctx.read() as session:
        rows = await load_rows(session, clock.now())
    rows = apply_filters(rows, status=status, flag=flag, q=q)
    return render(
        request,
        "participants.html",
        admin,
        rows=rows,
        statuses=STATUS_LABELS,
        filters=FILTERS,
        status=status,
        flag=flag,
        q=q,
        cell_titles=CELL_TITLES,
        days=DAYS,
    )


@dataclass
class ChatLine:
    message: Message
    text: str
    author: str | None
    unread: bool


def chat_text(message: Message) -> str:
    # Інваріант 8: текст інструкції в адмінці не показується.
    if message.kind == Kind.INSTRUCTION:
        return f"📄 Інструкція — день {message.session_day}"
    return message.text


async def _participant_or_404(session: AsyncSession, code: str) -> Participant:
    participant = (
        await session.execute(select(Participant).where(Participant.code == normalize_code(code)))
    ).scalar_one_or_none()
    if participant is None:
        raise HTTPException(status_code=404, detail="Учасника не знайдено")
    return participant


@router.get("/participants/{code}")
async def participant_card(request: Request, code: str, admin: AdminUser = Depends(current_admin)) -> Response:
    ctx = get_ctx(request)
    content = ctx.content
    async with ctx.read() as session:
        participant = await _participant_or_404(session, code)
        sessions = (
            await session.execute(
                select(StudySession).where(StudySession.participant_id == participant.id).order_by(StudySession.day)
            )
        ).scalars().all()
        answers = (
            await session.execute(
                select(CheckinAnswer).where(CheckinAnswer.session_id.in_([s.id for s in sessions]))
            )
        ).scalars().all()
        messages = (
            await session.execute(
                select(Message).where(Message.participant_id == participant.id).order_by(Message.created_at, Message.id)
            )
        ).scalars().all()
        authors = dict(
            (await session.execute(select(AdminUser.id, AdminUser.username))).tuples().all()
        )
        events = (
            await session.execute(
                select(Event).where(Event.participant_id == participant.id).order_by(Event.at.desc()).limit(30)
            )
        ).scalars().all()
        reminder_templates = (
            await session.execute(
                select(ReminderTemplate).where(ReminderTemplate.is_active.is_(True)).order_by(ReminderTemplate.title)
            )
        ).scalars().all()

    answers_by_session: dict[int, list[tuple[str, str]]] = {}
    for item in answers:
        question = content.checkin.by_id(item.question_id)
        label = dict(question.choices()).get(item.value, item.value) if question else item.value
        answers_by_session.setdefault(item.session_id, []).append((item.question_id, label))
    chat = [
        ChatLine(
            message=m,
            text=chat_text(m),
            author=authors.get(m.sender_admin_id) if m.sender_admin_id else None,
            unread=m.direction == "in" and m.read_by_admin_at is None,
        )
        for m in messages
    ]

    # Відкриття картки позначає вхідні прочитаними (підсвічені — ті, що були непрочитані до цього).
    if any(line.unread for line in chat):
        async with ctx.db() as session, session.begin():
            await session.execute(
                update(Message)
                .where(
                    Message.participant_id == participant.id,
                    Message.direction == "in",
                    Message.read_by_admin_at.is_(None),
                )
                .values(read_by_admin_at=clock.now())
            )

    return render(
        request,
        "participant.html",
        admin,
        participant=participant,
        sessions=sessions,
        answers=answers_by_session,
        chat=chat,
        events=events,
        reminder_templates=reminder_templates,
        can_message=participant.status not in NO_MESSAGES,
        can_withdraw=admin.role == "owner" and participant.status in WITHDRAWABLE,
        now=clock.now(),
    )


@router.post("/participants/{code}/message")
async def send_manual_message(
    request: Request, code: str, text: str = Form(""), admin: AdminUser = Depends(current_admin)
) -> Response:
    ctx = get_ctx(request)
    text = text.strip()
    async with ctx.read() as session:
        participant = await _participant_or_404(session, code)
        chat_id = await chat_of(session, participant.id)
    url = f"/participants/{participant.code}"
    if not text:
        flash(request, "Повідомлення порожнє.", "error")
        return redirect(url)
    if participant.status in NO_MESSAGES:
        flash(request, "Учасник вибув або не погодився — писати йому не можна.", "error")
        return redirect(url)
    if chat_id is None:
        flash(request, "Контакт учасника видалено — надіслати неможливо.", "error")
        return redirect(url)

    async def audit(session: AsyncSession) -> None:
        log_event(session, "admin_message_sent", actor=admin_actor(admin.id), participant_id=participant.id)

    result = await ctx.sender.send(
        chat_id, text, participant_id=participant.id, kind=Kind.MANUAL, sender_admin_id=admin.id, after=audit
    )
    if result.ok:
        flash(request, "Повідомлення надіслано.")
    elif result.blocked:
        flash(request, "Учасник заблокував бота — повідомлення не доставлено.", "error")
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

