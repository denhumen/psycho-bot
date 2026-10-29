from datetime import timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response
from sqlalchemy import func, select

from app.admin.auth import get_ctx
from app.admin.web import redirect, render
from app.clock import clock
from app.db.models import AdminUser, Event
from app.domain.admins import by_username, verify_password
from app.domain.events import admin_actor, log_event

router = APIRouter()

LOGIN_ATTEMPTS = 5
LOGIN_WINDOW = timedelta(minutes=15)


@router.get("/login")
async def login_form(request: Request) -> Response:
    if request.session.get("admin_id"):
        return redirect("/")
    return render(request, "login.html")


@router.post("/login")
async def login(request: Request, username: str = Form(""), password: str = Form("")) -> Response:
    ctx = get_ctx(request)
    username = username.strip()
    async with ctx.read() as session:
        # Ліміт: 5 невдалих спроб на username за 15 хв (лічильник у БД — рестарт його не скидає).
        failures = (
            await session.execute(
                select(func.count())
                .select_from(Event)
                .where(
                    Event.type == "admin_login_failed",
                    func.json_extract(Event.payload, "$.username") == username,
                    Event.at >= clock.now() - LOGIN_WINDOW,
                )
            )
        ).scalar_one()
        admin = await by_username(session, username)
    if failures >= LOGIN_ATTEMPTS:
        return render(request, "login.html", error="Забагато спроб. Спробуйте через 15 хвилин.", status_code=429)

    valid = verify_password(admin.password_hash if admin else None, password)
    if admin is None or not admin.is_active or not valid:
        async with ctx.db() as session, session.begin():
            log_event(session, "admin_login_failed", username=username)
        return render(request, "login.html", error="Невірний логін або пароль.", status_code=401)

    async with ctx.db() as session, session.begin():
        stored = await session.get(AdminUser, admin.id)
        assert stored is not None
        stored.last_login_at = clock.now()
        log_event(session, "admin_login", actor=admin_actor(admin.id))
    request.session.clear()
    request.session["admin_id"] = admin.id
    return redirect("/")


@router.post("/logout")
async def logout(request: Request) -> Response:
    request.session.clear()
    return redirect("/login")
