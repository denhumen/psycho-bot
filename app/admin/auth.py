import secrets

from fastapi import Depends, HTTPException, Request

from app.context import AppContext
from app.db.models import AdminUser

SESSION_COOKIE = "study_admin"
SESSION_MAX_AGE_SECONDS = 12 * 3600


class LoginRequired(Exception):
    """Перехоплюється обробником у main → редирект на /login."""


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx  # type: ignore[no-any-return]


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return str(token)


async def csrf_protect(request: Request) -> None:
    """Підключено до всього роутера адмінки: кожен POST має містити csrf_token."""
    if request.method != "POST":
        return
    form = await request.form()
    sent = form.get("csrf_token")
    expected = request.session.get("csrf")
    if not expected or not isinstance(sent, str) or not secrets.compare_digest(sent, str(expected)):
        raise HTTPException(status_code=403, detail="CSRF-токен недійсний. Оновіть сторінку і спробуйте ще раз.")


async def current_admin(request: Request) -> AdminUser:
    admin_id = request.session.get("admin_id")
    if admin_id is None:
        raise LoginRequired
    async with get_ctx(request).read() as session:
        admin = await session.get(AdminUser, admin_id)
    if admin is None or not admin.is_active:
        request.session.clear()
        raise LoginRequired
    return admin


async def require_owner(admin: AdminUser = Depends(current_admin)) -> AdminUser:
    if admin.role != "owner":
        raise HTTPException(status_code=403, detail="Доступно лише ролі owner.")
    return admin
