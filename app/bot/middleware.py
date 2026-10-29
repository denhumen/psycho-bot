import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Chat, TelegramObject, Update, User
from sqlalchemy.dialects.sqlite import insert

from app.clock import clock
from app.context import AppContext
from app.db.models import ProcessedUpdate
from app.domain.events import BOT, log_event
from app.domain.participants import by_chat

log = logging.getLogger(__name__)


class DeduplicateUpdatesMiddleware(BaseMiddleware):
    """Пропускає апдейти, які вже були повністю оброблені до рестарту.
    update_id фіксується після обробки: перерваний посередині апдейт обробиться ще раз."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        assert isinstance(event, Update)
        async with self.ctx.read() as session:
            if await session.get(ProcessedUpdate, event.update_id) is not None:
                log.info("Апдейт %s уже оброблено — пропускаю", event.update_id)
                return None
        result = await handler(event, data)
        async with self.ctx.db() as session, session.begin():
            await session.execute(
                insert(ProcessedUpdate).values(update_id=event.update_id, created_at=clock.now()).on_conflict_do_nothing()
            )
        return result


class PrivateActivityMiddleware(BaseMiddleware):
    """Ігнорує все, крім приватних чатів (бот може бути в чатах дайджесту/бекапу).
    Для зареєстрованого учасника оновлює last_activity_at і знімає bot_blocked."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat: Chat | None = data.get("event_chat")
        user: User | None = data.get("event_from_user")
        if user is None or (chat is not None and chat.type != "private"):
            return None
        async with self.ctx.db() as session, session.begin():
            participant = await by_chat(session, user.id)
            if participant is not None:
                participant.last_activity_at = clock.now()
                if participant.bot_blocked:
                    participant.bot_blocked = False
                    log_event(session, "bot_unblocked", actor=BOT, participant_id=participant.id)
        return await handler(event, data)
