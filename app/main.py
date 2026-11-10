import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from aiogram import Bot
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.admin.auth import SESSION_COOKIE, SESSION_MAX_AGE_SECONDS, LoginRequired
from app.admin.routes import build_router as build_admin_router
from app.bot.router import build_dispatcher, create_bot, run_polling
from app.bot.sender import Sender
from app.config import Settings
from app.context import AppContext
from app.db.migrate import run_migrations
from app.db.session import create_engine, create_read_sessionmaker, create_sessionmaker
from app.domain.allocation import ensure_slots
from app.domain.content import load_content
from app.logging_setup import configure_logging
from app.scheduler.loop import Scheduler

log = logging.getLogger(__name__)

SCHEDULER_STALE_SECONDS = 120
SHUTDOWN_TIMEOUT_SECONDS = 20


def create_app(settings: Settings | None = None, *, run_background: bool = True, bot: Bot | None = None) -> FastAPI:
    """run_background=False — без polling і планувальника (тести адмінки).
    bot — підмінений бот для тестів."""
    settings = settings or Settings()
    if run_background:
        settings.check_runtime()
    configure_logging(settings.log_level)
    # Невалідний контент — застосунок не стартує (ContentError з поясненням).
    content = load_content(settings.content_dir, settings.support_contact)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(run_migrations, settings.database_path)
        engine = create_engine(settings.database_path)
        db = create_sessionmaker(engine)
        telegram = bot or create_bot(settings.bot_token.get_secret_value())
        ctx = AppContext(
            settings=settings,
            engine=engine,
            db=db,
            read=create_read_sessionmaker(engine),
            content=content,
            sender=Sender(telegram, db, content),
        )
        app.state.ctx = ctx
        async with ctx.db() as session, session.begin():
            await ensure_slots(session, settings)
        if settings.dev_day_minutes:
            log.warning("DEV_DAY_MINUTES=%s: прискорений розклад, НЕ для продакшену", settings.dev_day_minutes)

        dp = build_dispatcher(ctx)
        scheduler = Scheduler(ctx)
        polling: asyncio.Task[None] | None = None
        ticking: asyncio.Task[None] | None = None
        if run_background:
            polling = asyncio.create_task(run_polling(dp, telegram), name="polling")
            ticking = asyncio.create_task(scheduler.run(), name="scheduler")
            ctx.scheduler_enabled = True
        log.info("Застосунок запущено")
        try:
            yield
        finally:
            # SIGTERM: зупиняємо polling, даємо планувальнику доробити поточний тік.
            if polling is not None:
                with contextlib.suppress(RuntimeError, TimeoutError):
                    await asyncio.wait_for(dp.stop_polling(), SHUTDOWN_TIMEOUT_SECONDS)
                polling.cancel()
                await asyncio.gather(polling, return_exceptions=True)
            if ticking is not None:
                scheduler.stop()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(ticking), SHUTDOWN_TIMEOUT_SECONDS)
                ticking.cancel()
                await asyncio.gather(ticking, return_exceptions=True)
            await telegram.session.close()
            await engine.dispose()
            log.info("Застосунок зупинено")

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    # Підписаний cookie сесії: HttpOnly завжди, Secure — за COOKIE_SECURE.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value() or "unset",
        session_cookie=SESSION_COOKIE,
        max_age=SESSION_MAX_AGE_SECONDS,
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    app.include_router(build_admin_router())

    @app.exception_handler(LoginRequired)
    async def _to_login(_request: Request, _exc: LoginRequired) -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        ctx: AppContext = request.app.state.ctx
        try:
            async with ctx.read() as session:
                await session.execute(text("SELECT 1"))
        except Exception:
            log.exception("healthz: БД недоступна")
            return JSONResponse({"status": "db_unavailable"}, status_code=503)
        if ctx.scheduler_enabled:
            last = ctx.scheduler_last_tick
            if last is None or time.monotonic() - last > SCHEDULER_STALE_SECONDS:
                return JSONResponse({"status": "scheduler_stale"}, status_code=503)
        return JSONResponse({"status": "ok"})

    return app


def __getattr__(name: str) -> Any:
    # `uvicorn app.main:app` — застосунок створюється ліниво, щоб імпорт модуля
    # у тестах не вимагав змінних оточення.
    if name == "app":
        application = create_app()
        globals()["app"] = application
        return application
    raise AttributeError(name)
