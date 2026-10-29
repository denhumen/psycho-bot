from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.admin.auth import csrf_token, get_ctx
from app.db.models import AdminUser

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


def render(
    request: Request, name: str, admin: AdminUser | None = None, status_code: int = 200, **context: Any
) -> HTMLResponse:
    ctx = get_ctx(request)
    tz = ctx.settings.tz

    def local(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
        return value.astimezone(tz).strftime(fmt) if value else "—"

    return templates.TemplateResponse(
        request,
        name,
        {
            "admin": admin,
            "csrf_token": csrf_token(request),
            "flashes": request.session.pop("flash", []),
            "local": local,
            "status_label": lambda status: STATUS_LABELS.get(status, status),
            **context,
        },
        status_code=status_code,
    )
