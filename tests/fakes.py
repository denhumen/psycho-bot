"""Фейковий Telegram: перехоплює всі виклики Bot API, нічого не шле в мережу."""

import asyncio
import itertools
from collections.abc import AsyncGenerator
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.dispatcher.dispatcher import Dispatcher
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    GetMe,
    GetUpdates,
    SendDocument,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, PhotoSize, Update, User

from app.bot.router import build_dispatcher
from app.clock import clock
from app.context import AppContext

TOKEN = "123456:TEST-token-not-real"
# Спільний лічильник: у справжньому Telegram message_id у чаті не повторюються і після рестарту бота.
_MESSAGE_IDS = itertools.count(1000)


class FakeTelegram(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.blocked: set[int] = set()
        # Винятки, які кинуть наступні виклики (по черзі), незалежно від методу.
        self.fail_next: list[Exception] = []

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.calls.append(method)
        if self.fail_next:
            raise self.fail_next.pop(0)
        if isinstance(method, (SendMessage, SendDocument)):
            if method.chat_id in self.blocked:
                raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")
            return Message(
                message_id=next(_MESSAGE_IDS),
                date=clock.now(),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=getattr(method, "text", None),
            )
        if isinstance(method, (AnswerCallbackQuery, EditMessageText)):
            return True
        if isinstance(method, GetMe):
            return User(id=123456, is_bot=True, first_name="StudyBot", username="study_test_bot")
        if isinstance(method, GetUpdates):
            await asyncio.sleep(0.01)  # «long polling» без апдейтів
            return []
        raise NotImplementedError(type(method).__name__)

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:
        raise NotImplementedError
        yield b""

    async def close(self) -> None:
        pass

    # --- зручності для тестів ---

    def sent(self, chat_id: int | None = None) -> list[SendMessage]:
        return [
            c for c in self.calls if isinstance(c, SendMessage) and (chat_id is None or c.chat_id == chat_id)
        ]

    def texts(self, chat_id: int | None = None) -> list[str]:
        return [c.text for c in self.sent(chat_id)]

    def last_markup(self, chat_id: int) -> InlineKeyboardMarkup:
        for call in reversed(self.sent(chat_id)):
            if isinstance(call.reply_markup, InlineKeyboardMarkup):
                return call.reply_markup
        raise AssertionError("клавіатур не надсилалось")

    def callback_answers(self) -> list[AnswerCallbackQuery]:
        return [c for c in self.calls if isinstance(c, AnswerCallbackQuery)]

    def clear(self) -> None:
        self.calls.clear()


def make_bot() -> Bot:
    return Bot(token=TOKEN, session=FakeTelegram())


def fake(ctx: AppContext) -> FakeTelegram:
    session = ctx.sender.bot.session
    assert isinstance(session, FakeTelegram)
    return session


class BotHarness:
    """Подає апдейти в той самий Dispatcher, що й у продакшені."""

    _update_ids = itertools.count(1)
    _message_ids = itertools.count(1)

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.dp: Dispatcher = build_dispatcher(ctx)
        self.tg = fake(ctx)

    @staticmethod
    def _user(chat_id: int) -> User:
        return User(id=chat_id, is_bot=False, first_name="Test")

    def _message(self, chat_id: int, **fields: Any) -> Message:
        return Message(
            message_id=fields.pop("message_id", next(self._message_ids)),
            date=clock.now(),
            chat=Chat(id=chat_id, type=fields.pop("chat_type", "private")),
            from_user=self._user(chat_id),
            **fields,
        )

    async def feed(self, update: Update) -> Any:
        return await self.dp.feed_update(self.ctx.sender.bot, update)

    async def text(self, chat_id: int, text: str, **fields: Any) -> Message:
        message = self._message(chat_id, text=text, **fields)
        await self.feed(Update(update_id=next(self._update_ids), message=message))
        return message

    async def photo(self, chat_id: int) -> None:
        photo = [PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)]
        await self.feed(Update(update_id=next(self._update_ids), message=self._message(chat_id, photo=photo)))

    async def press(self, chat_id: int, data: str, *, message_text: str = "…", message_id: int = 1) -> None:
        message = self._message(chat_id, text=message_text, message_id=message_id)
        callback = CallbackQuery(
            id=str(next(self._update_ids)),
            from_user=self._user(chat_id),
            chat_instance="ci",
            data=data,
            message=message,
        )
        await self.feed(Update(update_id=next(self._update_ids), callback_query=callback))
