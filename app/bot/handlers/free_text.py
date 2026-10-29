from datetime import timedelta

from aiogram import Router
from aiogram.enums import ContentType
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.registration import handle_code
from app.clock import clock
from app.context import AppContext
from app.db.models import Kind, Status
from app.db.models import Message as StoredMessage
from app.domain.events import BOT, log_event
from app.domain.participants import by_chat

ACK_INTERVAL = timedelta(hours=6)
ACK_KEYS = ("free_text_ack", "free_text_ack_active")
MEDIA_KEYS = ("media_not_supported",)


async def _replied_recently(session: AsyncSession, participant_id: int, keys: tuple[str, ...]) -> bool:
    last = (
        await session.execute(
            select(func.max(StoredMessage.created_at)).where(
                StoredMessage.participant_id == participant_id,
                StoredMessage.direction == "out",
                StoredMessage.text_key.in_(keys),
            )
        )
    ).scalar_one_or_none()
    return last is not None and clock.now() - last < ACK_INTERVAL


async def on_any_message(message: Message, ctx: AppContext) -> None:
    chat_id = message.chat.id
    is_text = message.text is not None
    reply_key = None
    async with ctx.db() as session, session.begin():
        participant = await by_chat(session, chat_id)
        if participant is not None:
            # Вміст медіа не зберігаємо — лише тип.
            stored_text = message.text if is_text else f"[медіа: {ContentType(message.content_type).value}]"
            result = await session.execute(
                insert(StoredMessage)
                .values(
                    participant_id=participant.id,
                    direction="in",
                    kind=Kind.PARTICIPANT_TEXT,
                    text=stored_text,
                    telegram_message_id=message.message_id,
                    created_at=clock.now(),
                )
                .on_conflict_do_nothing()
            )
            if result.rowcount == 1:
                if not is_text:
                    if not await _replied_recently(session, participant.id, MEDIA_KEYS):
                        reply_key = "media_not_supported"
                elif not await _replied_recently(session, participant.id, ACK_KEYS):
                    reply_key = "free_text_ack_active" if participant.status == Status.ACTIVE else "free_text_ack"

    if participant is None:
        # Будь-який текст від незареєстрованого chat_id — це спроба ввести код.
        if message.text and not message.text.startswith("/"):
            await handle_code(ctx, chat_id, message.text)
        else:
            await ctx.sender.send(chat_id, ctx.content.t("not_registered"))
        return
    if reply_key is not None:
        await ctx.sender.send(chat_id, ctx.content.t(reply_key), participant_id=participant.id, text_key=reply_key)


async def on_my_chat_member(event: ChatMemberUpdated, ctx: AppContext) -> None:
    """Telegram повідомляє, коли учасник блокує або розблоковує бота."""
    if event.chat.type != "private":
        return
    blocked = event.new_chat_member.status == "kicked"
    async with ctx.db() as session, session.begin():
        participant = await by_chat(session, event.chat.id)
        if participant is not None and participant.bot_blocked != blocked:
            participant.bot_blocked = blocked
            log_event(session, "bot_blocked" if blocked else "bot_unblocked", actor=BOT, participant_id=participant.id)


def make_router() -> Router:
    router = Router(name="free_text")
    router.message.register(on_any_message)
    router.my_chat_member.register(on_my_chat_member)
    return router
