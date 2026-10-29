import re
from collections.abc import Iterator
from contextlib import contextmanager

from aiogram import Bot
from fastapi.testclient import TestClient
from httpx import Response

from app.config import Settings
from app.context import AppContext
from app.domain.admins import create_admin
from app.main import create_app
from tests.fakes import FakeTelegram, make_bot

OWNER = ("owner", "owner-password-1")
RESEARCHER = ("researcher", "researcher-pass-1")

_CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')


@contextmanager
def admin_client(settings: Settings) -> Iterator[tuple[TestClient, FakeTelegram]]:
    bot: Bot = make_bot()
    app = create_app(settings, run_background=False, bot=bot)
    with TestClient(app, follow_redirects=False) as client:
        session = bot.session
        assert isinstance(session, FakeTelegram)
        yield client, session


async def seed_admins(ctx: AppContext) -> None:
    async with ctx.db() as s, s.begin():
        await create_admin(s, *OWNER, role="owner")
        await create_admin(s, *RESEARCHER, role="researcher")


def csrf(client: TestClient, path: str = "/login") -> str:
    match = _CSRF.search(client.get(path).text)
    assert match, f"на {path} немає csrf_token"
    return match.group(1)


def login(client: TestClient, username: str, password: str) -> Response:
    return client.post("/login", data={"csrf_token": csrf(client), "username": username, "password": password})


def post(client: TestClient, path: str, data: dict[str, str] | None = None, **kwargs: object) -> Response:
    """POST з валідним CSRF-токеном поточної сесії."""
    token = csrf(client, "/")
    return client.post(path, data={"csrf_token": token, **(data or {})}, **kwargs)  # type: ignore[arg-type]
