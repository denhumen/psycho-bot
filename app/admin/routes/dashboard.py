from dataclasses import dataclass

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response

from app.admin.auth import current_admin, get_ctx, require_owner
from app.admin.overview import DONE, MISSED, WAITING, Row, load_rows
from app.admin.web import flash, redirect, render
from app.clock import clock
from app.db.models import AdminUser, Status
from app.domain.events import admin_actor, log_event
from app.domain.sessions import DAYS
from app.domain.state import SCHEDULER_PAUSED, is_paused, set_value
from app.domain.stats import group_counts, local_day_bounds

router = APIRouter()


@dataclass
class DayAdherence:
    day: int
    sent: int
    done: int
    missed: int
    open: int
    base: int

    def pct(self, n: int) -> float:
        return round(n / self.base * 100, 1) if self.base else 0.0

    @property
    def rest_pct(self) -> float:
        return max(0.0, round(100 - self.pct(self.done) - self.pct(self.missed) - self.pct(self.open), 1))


def flow(rows: list[Row]) -> dict[str, int]:
    """Потік учасників у термінах CONSORT. Без груп: розбиття додає лише owner."""
    status = [r.participant.status for r in rows]
    randomized = [r for r in rows if r.participant.randomized_at is not None]
    withdrawn_after = [r for r in randomized if r.participant.status == Status.WITHDRAWN]
    return {
        "registered": len(rows),
        "no_consent_yet": status.count(Status.REGISTERED),
        "declined": status.count(Status.DECLINED),
        "consented": sum(1 for r in rows if r.participant.consented_at is not None),
        "not_started": status.count(Status.CONSENTED),
        "withdrawn_before": sum(
            1 for r in rows if r.participant.status == Status.WITHDRAWN and r.participant.randomized_at is None
            and r.participant.consented_at is not None
        ),
        "randomized": len(randomized),
        "in_week": sum(1 for r in randomized if r.participant.status == Status.ACTIVE),
        "completed": status.count(Status.COMPLETED),
        "withdrawn_after": len(withdrawn_after),
        "withdrawn_blocked": sum(1 for r in withdrawn_after if r.participant.bot_blocked),
    }


def adherence(rows: list[Row]) -> list[DayAdherence]:
    randomized = [r for r in rows if r.participant.randomized_at is not None]
    result = []
    for index in range(DAYS):
        cells = [r.cells[index] for r in randomized]
        sent = sum(1 for r in randomized if (s := r.sessions[index]) is not None and s.sent_at is not None)
        result.append(
            DayAdherence(
                day=index + 1,
                sent=sent,
                done=cells.count(DONE),
                missed=cells.count(MISSED),
                open=cells.count(WAITING),
                base=len(randomized),
            )
        )
    return result


@router.get("/")
async def dashboard(request: Request, admin: AdminUser = Depends(current_admin)) -> Response:
    ctx = get_ctx(request)
    now = clock.now()
    start, end = local_day_bounds(ctx.settings, now.astimezone(ctx.settings.tz).date())
    async with ctx.read() as session:
        rows = await load_rows(session, now)
        # Розбиття A/B — лише owner: за приростом лічильника researcher міг би вгадати групу.
        groups = await group_counts(session) if admin.role == "owner" else None
        paused = await is_paused(session)

    sent_today = done_today = open_today = 0
    for row in rows:
        for study_session, cell in zip(row.sessions, row.cells, strict=True):
            if study_session is not None and study_session.sent_at is not None and start <= study_session.sent_at < end:
                sent_today += 1
                done_today += cell == DONE
                open_today += cell == WAITING
    attention = {
        "unread": sum(1 for r in rows if r.unread),
        "not_done_today": sum(1 for r in rows if r.pending_now),
        "missed_streak": sum(1 for r in rows if r.missed_streak),
        "blocked": sum(1 for r in rows if r.participant.bot_blocked),
    }
    return await render(
        request,
        "dashboard.html",
        admin,
        now=now,
        paused=paused,
        attention=attention,
        today={"sent": sent_today, "done": done_today, "open": open_today},
        flow=flow(rows),
        groups=groups,
        days=adherence(rows),
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
