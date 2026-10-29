import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.clock import clock
from app.context import AppContext
from app.domain.events import log_event
from app.domain.state import is_paused
from app.scheduler.jobs import Jobs

log = logging.getLogger(__name__)

TICK_SECONDS = 30
# Задача, що впала, не запускається повторно стільки часу (щоб не засипати events).
JOB_RETRY_DELAY = timedelta(minutes=5)


@dataclass
class Job:
    name: str
    run: Callable[[], Awaitable[None]]
    # Автоматичні дії з учасниками — не виконуються на паузі.
    touches_participants: bool


class Scheduler:
    def __init__(self, ctx: AppContext, interval: float = TICK_SECONDS) -> None:
        self.ctx = ctx
        self.interval = interval
        self.jobs = Jobs(ctx)
        self.schedule = [
            Job("close_expired", self.jobs.close_expired, True),
            Job("send_due", self.jobs.send_due, True),
            Job("auto_reminders", self.jobs.auto_reminders, True),
            Job("finish_participants", self.jobs.finish_participants, True),
            Job("nightly_backup", self.jobs.nightly_backup, False),
            Job("daily_digest", self.jobs.daily_digest, False),
        ]
        self._stop = asyncio.Event()
        self._failed_until: dict[str, datetime] = {}

    async def tick(self) -> None:
        async with self.ctx.read() as session:
            paused = await is_paused(session)
        for job in self.schedule:
            if paused and job.touches_participants:
                continue
            retry_at = self._failed_until.get(job.name)
            if retry_at is not None and clock.now() < retry_at:
                continue
            try:
                await job.run()
                self._failed_until.pop(job.name, None)
            except Exception as exc:
                # Помилка однієї задачі не зупиняє інші й цикл.
                log.exception("Задача планувальника %s впала", job.name)
                self._failed_until[job.name] = clock.now() + JOB_RETRY_DELAY
                with contextlib.suppress(Exception):
                    async with self.ctx.db() as session, session.begin():
                        log_event(session, "send_failed", job=job.name, error=type(exc).__name__, detail=str(exc)[:300])
        self.ctx.scheduler_last_tick = time.monotonic()

    async def run(self) -> None:
        log.info("Планувальник запущено (тік %s с)", self.interval)
        while not self._stop.is_set():
            await self.tick()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), self.interval)
        log.info("Планувальник зупинено")

    def stop(self) -> None:
        """Поточний тік доробляється до кінця; наступний не починається."""
        self._stop.set()
