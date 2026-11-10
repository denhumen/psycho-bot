"""Єдина точка надсилання повідомлень: розбиття довгих текстів, RetryAfter,
мережеві ретраї, Forbidden → bot_blocked, лог у messages."""

import asyncio
import html
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.enums import ParseMode
from aiogram.methods import EditMessageText, SendDocument, SendMessage, TelegramMethod
from aiogram.types import (
    BufferedInputFile,
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MaybeInaccessibleMessage,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Kind, Message, Participant
from app.domain.content import Content, RichText
from app.domain.events import log_event

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096
MAX_ATTEMPTS = 4
MAX_RETRY_AFTER_SECONDS = 120

AfterSent = Callable[[AsyncSession], Awaitable[None]]


def _tg_len(text: str) -> int:
    # Telegram рахує довжину в UTF-16 code units.
    return len(text.encode("utf-16-le")) // 2


def _hard_split(text: str, limit: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    for line in text.split("\n"):
        while _tg_len(line) > limit:
            cut = limit
            while _tg_len(line[:cut]) > limit:
                cut -= 1
            pieces.append(line[:cut])
            line = line[cut:]
        candidate = line if not current else f"{current}\n{line}"
        if _tg_len(candidate) <= limit:
            current = candidate
        else:
            pieces.append(current)
            current = line
    if current:
        pieces.append(current)
    return pieces


def split_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Ділить текст по абзацах так, щоб кожен шматок був ≤ limit."""
    if _tg_len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        for piece in [paragraph] if _tg_len(paragraph) <= limit else _hard_split(paragraph, limit):
            candidate = piece if not current else f"{current}\n\n{piece}"
            if _tg_len(candidate) <= limit:
                current = candidate
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks


@dataclass
class SendResult:
    ok: bool
    message_ids: list[int] = field(default_factory=list)
    blocked: bool = False


def with_url_buttons(markup: InlineKeyboardMarkup | None, rich: RichText) -> InlineKeyboardMarkup | None:
    """Кнопки-посилання з texts.json — окремими рядками під кнопками, які задає код."""
    if not rich.buttons:
        return markup
    rows = list(markup.inline_keyboard) if markup else []
    rows += [[InlineKeyboardButton(text=b.text, url=b.url)] for b in rich.buttons]
    return InlineKeyboardMarkup(inline_keyboard=rows)


class Sender:
    def __init__(self, bot: Bot, db: async_sessionmaker[AsyncSession], content: Content | None = None) -> None:
        self.bot = bot
        self.db = db
        # Для розширених текстів texts.json (HTML і кнопки-посилання) за text_key.
        self.content = content
        self.sleep = asyncio.sleep  # тести підміняють, щоб не чекати RetryAfter

    async def call[T](self, method: TelegramMethod[T]) -> T:
        """Виклик Bot API з обробкою RetryAfter і тимчасових помилок."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return await self.bot(method)
            except TelegramRetryAfter as exc:
                if attempt == MAX_ATTEMPTS or exc.retry_after > MAX_RETRY_AFTER_SECONDS:
                    raise
                log.warning("Telegram RetryAfter %ss (%s)", exc.retry_after, type(method).__name__)
                await self.sleep(exc.retry_after)
            except (TelegramNetworkError, TelegramServerError) as exc:
                if attempt == MAX_ATTEMPTS:
                    raise
                log.warning("Тимчасова помилка Telegram (%s), спроба %s", type(exc).__name__, attempt)
                await self.sleep(2**attempt)
        raise AssertionError("unreachable")

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        participant_id: int | None = None,
        kind: str = Kind.SYSTEM,
        text_key: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
        session_day: int | None = None,
        sender_admin_id: int | None = None,
        after: AfterSent | None = None,
    ) -> SendResult:
        """Надсилає текст; після успіху в одній транзакції пише messages і виконує `after`.
        Порядок «надіслати → записати» дає at-least-once при жорсткому краші (див. DECISIONS.md)."""
        rich = self.content.rich(text_key) if self.content is not None else None
        parse_mode = ParseMode.HTML if rich else None
        if rich:
            reply_markup = with_url_buttons(reply_markup, rich)
        # Розширений текст не ділимо: розрізання могло б зламати HTML (довжину обмежує валідація).
        chunks = [text] if rich else split_text(text)
        message_ids: list[int] = []
        try:
            for index, chunk in enumerate(chunks):
                markup = reply_markup if index == len(chunks) - 1 else None
                sent = await self.call(
                    SendMessage(chat_id=chat_id, text=chunk, reply_markup=markup, parse_mode=parse_mode)
                )
                message_ids.append(sent.message_id)
        except TelegramForbiddenError:
            log.warning("Учасник %s заблокував бота", participant_id)
            if participant_id is not None:
                await self._mark_blocked(participant_id)
            return SendResult(ok=False, message_ids=message_ids, blocked=True)
        except TelegramAPIError as exc:
            log.error("Не вдалося надіслати (%s) учаснику %s: %s", kind, participant_id, type(exc).__name__)
            async with self.db() as session, session.begin():
                log_event(
                    session,
                    "send_failed",
                    participant_id=participant_id,
                    kind=kind,
                    error=type(exc).__name__,
                    detail=str(exc)[:300],
                )
            return SendResult(ok=False, message_ids=message_ids)

        if participant_id is not None or after is not None:
            async with self.db() as session, session.begin():
                if participant_id is not None:
                    session.add(
                        Message(
                            participant_id=participant_id,
                            direction="out",
                            kind=kind,
                            sender_admin_id=sender_admin_id,
                            text=text,
                            text_key=text_key,
                            telegram_message_id=message_ids[-1],
                            session_day=session_day,
                        )
                    )
                if after is not None:
                    await after(session)
        return SendResult(ok=True, message_ids=message_ids)

    async def send_document(self, chat_id: int, filename: str, data: bytes, caption: str) -> None:
        await self.call(SendDocument(chat_id=chat_id, document=BufferedInputFile(data, filename), caption=caption))

    async def mark_choice(self, chat_id: int, message: MaybeInaccessibleMessage | None, label: str) -> None:
        """Після натискання кнопки: дописати обраний варіант і прибрати кнопки-дії.
        Форматування і кнопки-посилання (з texts.json) зберігаються."""
        if message is None or isinstance(message, InaccessibleMessage) or not message.text:
            return
        rows = message.reply_markup.inline_keyboard if message.reply_markup else []
        links = [row for row in ([b for b in row if b.url] for row in rows) if row]
        try:
            await self.call(
                EditMessageText(
                    chat_id=chat_id,
                    message_id=message.message_id,
                    text=f"{message.html_text}\n\n✅ {html.escape(label, quote=False)}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=links) if links else None,
                )
            )
        except TelegramAPIError:
            # Повідомлення застаре, уже відредаговане тощо — для учасника це не критично.
            log.info("Не вдалося відредагувати повідомлення з кнопками")

    async def _mark_blocked(self, participant_id: int) -> None:
        async with self.db() as session, session.begin():
            participant = await session.get(Participant, participant_id)
            if participant is not None and not participant.bot_blocked:
                participant.bot_blocked = True
                log_event(session, "bot_blocked", participant_id=participant_id)
