from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy import func, select

from app.admin.auth import current_admin, get_ctx, require_owner
from app.admin.web import flash, redirect, render
from app.clock import clock
from app.db.models import AdminUser, AllocationSlot, Event, Participant
from app.domain.events import admin_actor, log_event
from app.domain.state import LAST_BACKUP_AT, SCHEDULER_PAUSED, get_time, is_paused, set_value
from app.domain.stats import awaiting_checkin, day_counts, group_counts, local_day_bounds, status_counts, unread_count

router = APIRouter()


@router.get("/")
async def dashboard(request: Request, admin: AdminUser = Depends(current_admin)) -> Response:
    ctx = get_ctx(request)
    now = clock.now()
    today = now.astimezone(ctx.settings.tz).date()
    async with ctx.read() as session:
        counts = await status_counts(session)
        randomized = (
            await session.execute(
                select(func.count()).select_from(AllocationSlot).where(AllocationSlot.participant_id.is_not(None))
            )
        ).scalar_one()
        # Розбиття A/B — лише owner: за приростом лічильника researcher міг би вгадати групу.
        groups = await group_counts(session) if admin.role == "owner" else None
        day = await day_counts(session, *local_day_bounds(ctx.settings, today))
        awaiting = await awaiting_checkin(session, now)
        unread = await unread_count(session)
        conflicts = (
            await session.execute(
                select(Event, Participant.code)
                .join(Participant, Participant.id == Event.participant_id)
                .where(Event.type == "code_conflict")
                .order_by(Event.at.desc())
                .limit(10)
            )
        ).all()
        conflicts_total = (
            await session.execute(select(func.count()).select_from(Event).where(Event.type == "code_conflict"))
        ).scalar_one()
        blocked = (
            await session.execute(
                select(func.count()).select_from(Participant).where(Participant.bot_blocked.is_(True))
            )
        ).scalar_one()
        last_backup = await get_time(session, LAST_BACKUP_AT)
        paused = await is_paused(session)
    return render(
        request,
        "dashboard.html",
        admin,
        counts=counts,
        total=sum(counts.values()),
        randomized=randomized,
        groups=groups,
        day=day,
        awaiting=awaiting,
        unread=unread,
        conflicts=conflicts,
        conflicts_total=conflicts_total,
        blocked=blocked,
        last_backup=last_backup,
        paused=paused,
    )


@router.post("/pause")
async def toggle_pause(
    request: Request, paused: str = Form(...), admin: AdminUser = Depends(require_owner)
) -> Response:
    ctx = get_ctx(request)
    pause = paused == "true"
    async with ctx.db() as session, session.begin():
        if await is_paused(session) != pause:
            await set_value(session, SCHEDULER_PAUSED, "true" if pause else "false")
            log_event(session, "scheduler_paused" if pause else "scheduler_resumed", actor=admin_actor(admin.id))
    flash(request, "Автоматичну розсилку призупинено." if pause else "Автоматичну розсилку відновлено.")
    return redirect("/")
