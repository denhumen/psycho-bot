from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from app.bot.sender import TELEGRAM_LIMIT, split_text
from app.context import AppContext
from app.db.models import Event, Kind, Message, Participant
from tests.conftest import add_participant
from tests.fakes import FakeTelegram

CHAT = 7001
_METHOD = SendMessage(chat_id=CHAT, text="x")


async def no_sleep(_: float) -> None:
    return None


def test_split_short_text_untouched() -> None:
    assert split_text("Привіт\n\nсвіт") == ["Привіт\n\nсвіт"]


def test_split_by_paragraphs() -> None:
    paragraphs = [f"Абзац {i} " + "я" * 1500 for i in range(6)]
    text = "\n\n".join(paragraphs)
    chunks = split_text(text)
    assert all(len(c) <= TELEGRAM_LIMIT for c in chunks)
    assert len(chunks) == 3
    assert "\n\n".join(chunks) == text  # жоден абзац не розірвано


def test_split_giant_paragraph() -> None:
    text = "слово " * 2000
    chunks = split_text(text)
    assert all(len(c) <= TELEGRAM_LIMIT for c in chunks)
    assert "".join(chunks) == text


async def test_long_message_logged_once_with_markup_on_last(ctx: AppContext, tg: FakeTelegram) -> None:
    pid = await add_participant(ctx.db, "LONG01")
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="ok", callback_data="ok")]])
    text = "\n\n".join("я" * 3000 for _ in range(3))
    result = await ctx.sender.send(CHAT, text, participant_id=pid, kind=Kind.INSTRUCTION, reply_markup=markup)
    assert result.ok and len(result.message_ids) == 3
    sent = tg.sent(CHAT)
    assert [m.reply_markup for m in sent[:-1]] == [None, None] and sent[-1].reply_markup == markup
    async with ctx.read() as s:
        messages = (await s.execute(select(Message))).scalars().all()
    assert len(messages) == 1
    assert messages[0].text == text and messages[0].telegram_message_id == result.message_ids[-1]


async def test_retry_after_and_network_errors_are_retried(ctx: AppContext, tg: FakeTelegram) -> None:
    ctx.sender.sleep = no_sleep
    tg.fail_next += [
        TelegramRetryAfter(method=_METHOD, message="Too Many Requests", retry_after=3),
        TelegramNetworkError(method=_METHOD, message="timeout"),
    ]
    result = await ctx.sender.send(CHAT, "привіт")
    assert result.ok
    assert len(tg.sent(CHAT)) == 3  # дві невдалі спроби + успішна


async def test_forbidden_marks_bot_blocked(ctx: AppContext, tg: FakeTelegram) -> None:
    pid = await add_participant(ctx.db, "BLOCK1")
    tg.blocked.add(CHAT)
    result = await ctx.sender.send(CHAT, "привіт", participant_id=pid)
    assert not result.ok and result.blocked
    await ctx.sender.send(CHAT, "ще раз", participant_id=pid)
    async with ctx.read() as s:
        participant = await s.get(Participant, pid)
        events = (await s.execute(select(Event.type).where(Event.participant_id == pid))).scalars().all()
        logged = (await s.execute(select(Message))).scalars().all()
    assert participant is not None and participant.bot_blocked
    assert events == ["bot_blocked"]  # подія лише при зміні прапорця
    assert logged == []


async def test_permanent_error_logs_send_failed(ctx: AppContext, tg: FakeTelegram) -> None:
    pid = await add_participant(ctx.db, "FAIL01")
    tg.fail_next.append(TelegramBadRequest(method=_METHOD, message="Bad Request: chat not found"))
    called = []

    async def after(_session: object) -> None:
        called.append(True)

    result = await ctx.sender.send(CHAT, "привіт", participant_id=pid, kind=Kind.MANUAL, after=after)
    assert not result.ok and not called
    async with ctx.read() as s:
        event = (await s.execute(select(Event).where(Event.type == "send_failed"))).scalar_one()
    assert event.participant_id == pid
    assert event.payload["kind"] == Kind.MANUAL
    assert "chat_id" not in event.payload
