from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.admin.auth import csrf_token, get_ctx
from app.clock import clock
from app.db.models import AdminUser
from app.domain.stats import unread_count

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

STATUS_LABELS = {
    "registered": "зареєстрований",
    "consented": "дав згоду",
    "active": "активний",
    "completed": "завершив",
    "declined": "не погодився",
    "withdrawn": "вибув",
}


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, message])


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


async def render(
    request: Request, name: str, admin: AdminUser | None = None, status_code: int = 200, **context: Any
) -> HTMLResponse:
    ctx = get_ctx(request)
    tz = ctx.settings.tz
    nav_unread = 0
    if admin is not None:
        async with ctx.read() as session:
            nav_unread = await unread_count(session)

    def local(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
        return value.astimezone(tz).strftime(fmt) if value else "—"

    def ago(value: datetime | None) -> str:
        """Відносний час для списків: «5 хв тому», «вчора», інакше дата."""
        if value is None:
            return "—"
        seconds = (clock.now() - value).total_seconds()
        if seconds < 60:
            return "щойно"
        if seconds < 3600:
            return f"{int(seconds // 60)} хв тому"
        if seconds < 86400:
            return f"{int(seconds // 3600)} год тому"
        if seconds < 2 * 86400:
            return "вчора"
        return local(value, "%d.%m.%Y")

    return templates.TemplateResponse(
        request,
        name,
        {
            "admin": admin,
            "csrf_token": csrf_token(request),
            "flashes": request.session.pop("flash", []),
            "local": local,
            "ago": ago,
            "status_label": lambda status: STATUS_LABELS.get(status, status),
            "nav_unread": nav_unread,
            "path": request.url.path,
            **context,
        },
        status_code=status_code,
    )
