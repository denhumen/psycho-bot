"""Inline-клавіатури. Кожна callback_data самодостатня:
consent:yes|no, time:HH:MM, ready, cs:{session_id}, ci:{session_id}:{question_id}:{value}, stop:yes|no."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.domain.content import ChoiceQuestion, Content, ScaleQuestion

SCALE_PER_ROW = 5
TIMES_PER_ROW = 3


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def consent(content: Content) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button(content.t("btn_consent_yes"), "consent:yes")],
            [_button(content.t("btn_consent_no"), "consent:no")],
        ]
    )


def reminder_times(options: list[str]) -> InlineKeyboardMarkup:
    buttons = [_button(option, f"time:{option}") for option in options]
    rows = [buttons[i : i + TIMES_PER_ROW] for i in range(0, len(buttons), TIMES_PER_ROW)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ready(content: Content) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_button(content.t("btn_ready"), "ready")]])


def checkin_start(content: Content, session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_button(content.t("btn_checkin"), f"cs:{session_id}")]])


def question(q: ChoiceQuestion | ScaleQuestion, session_id: int) -> InlineKeyboardMarkup:
    buttons = [_button(label, f"ci:{session_id}:{q.id}:{value}") for value, label in q.choices()]
    if isinstance(q, ScaleQuestion):
        rows = [buttons[i : i + SCALE_PER_ROW] for i in range(0, len(buttons), SCALE_PER_ROW)]
    else:
        rows = [[button] for button in buttons]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def stop(content: Content) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button(content.t("btn_stop_yes"), "stop:yes")],
            [_button(content.t("btn_stop_no"), "stop:no")],
        ]
    )
