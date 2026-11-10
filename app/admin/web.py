from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.admin.auth import csrf_token, get_ctx
from app.admin.overview import CELL_TITLES, DONE, MISSED, NOT_SENT, WAITING
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
ROLE_LABELS = {"owner": "Власник", "researcher": "Дослідник"}
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")
MONTHS_GEN = ("січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня",
              "жовтня", "листопада", "грудня")
WEEKDAYS_FULL = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд")


def plural(n: int, one: str, few: str, many: str) -> str:
    """Українські форми множини: 1 нове, 2 нові, 5 нових."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


templates.env.globals.update(
    WEEK_CLASS={DONE: "w-done", MISSED: "w-miss", WAITING: "w-wait", NOT_SENT: "w-none"},
    WEEK_GLYPH={DONE: "✓", MISSED: "✕", WAITING: "", NOT_SENT: ""},
    CELL_TITLES=CELL_TITLES,
    ROLE_LABELS=ROLE_LABELS,
)
templates.env.filters["plural"] = lambda n, one, few, many: plural(int(n), one, few, many)


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, message])


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def safe_next(value: str | None, default: str) -> str:
    """Лише локальні шляхи адмінки (без схеми і хоста) — захист від open redirect."""
    if not value:
        return default
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or not value.startswith("/") or value.startswith("//"):
        return default
    return value


async def render(
    request: Request, name: str, admin: AdminUser | None = None, status_code: int = 200, **context: Any
) -> HTMLResponse:
    ctx = get_ctx(request)
    tz = ctx.settings.tz
    nav_unread = 0
    if admin is not None:
        async with ctx.read() as session:
            nav_unread = await unread_count(session)

    def local(value: datetime | None, fmt: str = "%d.%m.%Y, %H:%M") -> str:
        return value.astimezone(tz).strftime(fmt) if value else "—"

    def short_day(value: date | datetime) -> str:
        """«вт, 03.11»"""
        day = value.astimezone(tz).date() if isinstance(value, datetime) else value
        return f"{WEEKDAYS[day.weekday()]}, {day:%d.%m}"

    def long_day(value: datetime) -> str:
        """«Чт, 5 листопада»"""
        day = value.astimezone(tz).date()
        return f"{WEEKDAYS_FULL[day.weekday()]}, {day.day} {MONTHS_GEN[day.month - 1]}"

    def ago(value: datetime | None) -> str:
        if value is None:
            return "—"
        seconds = (clock.now() - value).total_seconds()
        if seconds < 60:
            return "щойно"
        if seconds < 3600:
            return f"{int(seconds // 60)} хв тому"
        if seconds < 86400:
            return f"{int(seconds // 3600)} год тому"
        days = int(seconds // 86400)
        if days == 1:
            return "вчора"
        if days < 7:
            return f"{days} {plural(days, 'день', 'дні', 'днів')} тому"
        return local(value, "%d.%m.%Y")

    return templates.TemplateResponse(
        request,
        name,
        {
            "admin": admin,
            "csrf_token": csrf_token(request),
            "flashes": request.session.pop("flash", []),
            "local": local,
            "short_day": short_day,
            "long_day": long_day,
            "ago": ago,
            "status_label": lambda status: STATUS_LABELS.get(status, status),
            "status_title": lambda status: STATUS_LABELS.get(status, status).capitalize(),
            "nav_unread": nav_unread,
            "path": request.url.path,
            **context,
        },
        status_code=status_code,
    )
