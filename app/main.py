import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import Settings
from app.context import AppContext
from app.db.migrate import run_migrations
from app.db.session import create_engine, create_sessionmaker
from app.domain.allocation import ensure_slots
from app.logging_setup import configure_logging

log = logging.getLogger(__name__)

SCHEDULER_STALE_SECONDS = 120


def create_app(settings: Settings | None = None, *, run_background: bool = True) -> FastAPI:
    """run_background=False — без бота і планувальника (для тестів адмінки)."""
    settings = settings or Settings()
    if run_background:
        settings.check_runtime()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        await asyncio.to_thread(run_migrations, settings.database_path)
        engine = create_engine(settings.database_path)
        ctx = AppContext(settings=settings, engine=engine, db=create_sessionmaker(engine))
        app.state.ctx = ctx
        async with ctx.db() as session, session.begin():
            await ensure_slots(session, settings)
        if settings.dev_day_minutes:
            log.warning("DEV_DAY_MINUTES=%s: прискорений розклад, НЕ для продакшену", settings.dev_day_minutes)
        log.info("Застосунок запущено")
        try:
            yield
        finally:
            await engine.dispose()
            log.info("Застосунок зупинено")

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        ctx: AppContext = request.app.state.ctx
        try:
            async with ctx.db() as session:
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
