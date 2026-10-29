import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.backoff import BackoffConfig

from app.bot.handlers import consent, free_text, registration, start, time
from app.bot.middleware import DeduplicateUpdatesMiddleware, PrivateActivityMiddleware
from app.context import AppContext

log = logging.getLogger(__name__)

POLLING_BACKOFF = BackoffConfig(min_delay=1.0, max_delay=60.0, factor=1.5, jitter=0.1)


def create_bot(token: str) -> Bot:
    # Без parse_mode: усі тексти (зокрема ручні з адмінки) надсилаються як є.
    return Bot(token=token, default=DefaultBotProperties(parse_mode=None))


def build_dispatcher(ctx: AppContext) -> Dispatcher:
    # FSM вимкнено: стан учасника — тільки в БД.
    dp = Dispatcher(disable_fsm=True)
    dp["ctx"] = ctx
    dp.update.outer_middleware(DeduplicateUpdatesMiddleware(ctx))
    middleware = PrivateActivityMiddleware(ctx)
    dp.message.outer_middleware(middleware)
    dp.callback_query.outer_middleware(middleware)
    dp.include_routers(
        start.make_router(),
        consent.make_router(),
        time.make_router(),
        registration.make_router(),
        free_text.make_router(),  # останнім: ловить усе інше
    )
    return dp


async def run_polling(dp: Dispatcher, bot: Bot) -> None:
    """Long polling, що не падає: помилки getUpdates (зокрема TelegramConflictError, поки
    старий інстанс ще поллить під час деплою) aiogram ретраїть з backoff до 60 с; помилки
    старту (getMe) ретраїмо тут."""
    delay = 1.0
    while True:
        try:
            await dp.start_polling(
                bot,
                handle_signals=False,
                close_bot_session=False,
                handle_as_tasks=False,
                backoff_config=POLLING_BACKOFF,
                allowed_updates=dp.resolve_used_update_types(),
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Polling впав (%s), повтор через %.0f с", type(exc).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
