"""Завантаження й валідація content/*.json. Невалідний контент — застосунок не стартує."""

import html
import json
import string
from html.parser import HTMLParser
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, StringConstraints, ValidationError, model_validator

from app.domain.sessions import DAYS

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
# Ідентифікатори йдуть у callback_data (ліміт Telegram — 64 байти).
Ident = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,20}$")]

TODO_MARKER = "TODO"


class ContentError(RuntimeError):
    pass


# --- інструкції ------------------------------------------------------------


class Instructions(RootModel[dict[str, dict[str, NonEmpty]]]):
    @model_validator(mode="after")
    def _shape(self) -> "Instructions":
        if set(self.root) != {"A", "B"}:
            raise ValueError(f"мають бути рівно групи A і B, знайдено: {sorted(self.root)}")
        expected = {str(day) for day in range(1, DAYS + 1)}
        for group, days in self.root.items():
            if set(days) != expected:
                raise ValueError(f"група {group}: мають бути дні 1–{DAYS}, знайдено: {sorted(days, key=str)}")
        return self

    def text(self, group: str, day: int) -> str:
        return self.root[group][str(day)]


# --- чек-ін ----------------------------------------------------------------


class Option(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Ident
    label: NonEmpty


class ChoiceQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Ident
    type: Literal["choice"]
    text: NonEmpty
    # Коротка назва для адмінки й експорту («Завдання»); учасники її не бачать.
    label: NonEmpty | None = None
    options: list[Option] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_values(self) -> "ChoiceQuestion":
        values = [option.value for option in self.options]
        if len(values) != len(set(values)):
            raise ValueError(f"питання {self.id}: значення варіантів мають бути унікальні")
        return self

    def choices(self) -> list[tuple[str, str]]:
        return [(option.value, option.label) for option in self.options]


class ScaleQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Ident
    type: Literal["scale"]
    text: NonEmpty
    label: NonEmpty | None = None
    min: int
    max: int

    @model_validator(mode="after")
    def _range(self) -> "ScaleQuestion":
        if not self.min < self.max or self.max - self.min > 20:
            raise ValueError(f"питання {self.id}: має бути min < max і не більше 21 кнопки")
        return self

    def choices(self) -> list[tuple[str, str]]:
        return [(str(v), str(v)) for v in range(self.min, self.max + 1)]


Question = Annotated[ChoiceQuestion | ScaleQuestion, Field(discriminator="type")]


class Checkin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: list[Question] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> "Checkin":
        ids = [q.id for q in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("id питань мають бути унікальні")
        return self

    def by_id(self, question_id: str) -> ChoiceQuestion | ScaleQuestion | None:
        return next((q for q in self.questions if q.id == question_id), None)


# --- UI-тексти -------------------------------------------------------------

PLACEHOLDERS = {"support", "time", "date", "url", "day", "today"}
REQUIRED_PLACEHOLDERS = {
    "code_not_found": {"support"},
    "time_explanation": {"time"},
    "started": {"date", "time"},
    "final_message": {"url"},
    "help": {"support"},
    "time_changed": {"time"},
    "status_ready": {"time"},
    "status_waiting": {"date", "time"},
    "status_active": {"day", "today", "time"},
    "status_completed": {"url"},
}


# Розмітка Telegram (parse_mode=HTML), дозволена в «розширених» текстах.
TELEGRAM_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "blockquote", "tg-spoiler"}
URL_PREFIXES = ("https://", "http://", "tg://")
MAX_RICH_LENGTH = 4000
MAX_BUTTONS = 8


class _TelegramHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.errors: list[str] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in TELEGRAM_TAGS:
            self.errors.append(f"тег <{tag}> не підтримується Telegram (дозволені: {', '.join(sorted(TELEGRAM_TAGS))})")
            return
        names = {name for name, _ in attrs}
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if names != {"href"} or not href.startswith(URL_PREFIXES):
                self.errors.append("посилання має бути <a href=\"https://...\">текст</a>")
        elif names:
            self.errors.append(f"тег <{tag}> не може мати атрибутів")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"зайвий або неправильно вкладений </{tag}>")
        else:
            self.stack.pop()

    def close(self) -> None:
        super().close()
        if self.stack:
            self.errors.append(f"незакритий тег <{self.stack[-1]}>")


class _Plain(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_text(markup: str) -> str:
    """HTML-розмітка Telegram → звичайний текст (для показу в адмінці)."""
    parser = _Plain()
    parser.feed(markup)
    parser.close()
    return "".join(parser.parts)


def telegram_html_errors(text: str) -> list[str]:
    parser = _TelegramHtml()
    parser.feed(text)
    parser.close()
    return parser.errors


class UrlButton(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    url: str

    @model_validator(mode="after")
    def _url(self) -> "UrlButton":
        if not self.url.startswith(URL_PREFIXES):
            raise ValueError(f"кнопка «{self.text}»: url має починатися з https://, http:// або tg://")
        return self


class RichText(BaseModel):
    """Текст з HTML-розміткою Telegram і кнопками-посиланнями під повідомленням."""

    model_config = ConfigDict(extra="forbid")
    text: NonEmpty
    buttons: list[UrlButton] = Field(default_factory=list, max_length=MAX_BUTTONS)

    @model_validator(mode="after")
    def _markup(self) -> "RichText":
        errors = telegram_html_errors(self.text)
        if errors:
            raise ValueError("; ".join(errors))
        if len(self.text) > MAX_RICH_LENGTH:
            raise ValueError(f"текст довший за {MAX_RICH_LENGTH} символів")
        return self


TextValue = NonEmpty | RichText
# Підписи кнопок і фрагменти, що вставляються в інші тексти, — лише звичайний рядок.
PLAIN_ONLY_PREFIXES = ("btn_", "status_today_")


class Texts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    welcome_ask_code: TextValue
    not_registered: TextValue
    code_invalid_format: TextValue
    code_not_found: TextValue
    code_conflict: TextValue
    consent: TextValue
    btn_consent_yes: TextValue
    btn_consent_no: TextValue
    consent_declined: TextValue
    choose_time: TextValue
    time_explanation: TextValue
    btn_ready: TextValue
    started: TextValue
    btn_checkin: TextValue
    checkin_thanks: TextValue
    checkin_expired: TextValue
    checkin_already_done: TextValue
    auto_reminder: TextValue
    final_message: TextValue
    help: TextValue
    stop_confirm: TextValue
    btn_stop_yes: TextValue
    btn_stop_no: TextValue
    stopped: TextValue
    stop_cancelled: TextValue
    stop_unavailable: TextValue
    time_choose_new: TextValue
    time_changed: TextValue
    time_unavailable: TextValue
    free_text_ack: TextValue
    free_text_ack_active: TextValue
    media_not_supported: TextValue
    status_registered: TextValue
    status_consented: TextValue
    status_ready: TextValue
    status_waiting: TextValue
    status_active: TextValue
    status_today_done: TextValue
    status_today_pending: TextValue
    status_today_missed: TextValue
    status_completed: TextValue
    status_declined: TextValue
    status_withdrawn: TextValue
    button_outdated: TextValue

    @model_validator(mode="after")
    def _placeholders(self) -> "Texts":
        errors = []
        for key, value in self:
            if isinstance(value, RichText) and key.startswith(PLAIN_ONLY_PREFIXES):
                errors.append(f"{key}: тут дозволено лише звичайний рядок, без розмітки й кнопок")
                continue
            template = value.text if isinstance(value, RichText) else value
            try:
                used = {field for _, field, _, _ in string.Formatter().parse(template) if field is not None}
            except ValueError as exc:
                errors.append(f"{key}: некоректні фігурні дужки ({exc})")
                continue
            unknown = used - PLACEHOLDERS
            if unknown:
                errors.append(f"{key}: невідомі підстановки {sorted(unknown)}; дозволені {sorted(PLACEHOLDERS)}")
            missing = REQUIRED_PLACEHOLDERS.get(key, set()) - used
            if missing:
                errors.append(f"{key}: бракує підстановок {sorted(missing)}")
        if errors:
            raise ValueError("; ".join(errors))
        return self


class Content(BaseModel):
    instructions: Instructions
    checkin: Checkin
    texts: Texts
    support_contact: str = ""

    def t(self, key: str, **values: Any) -> str:
        value: str | RichText = getattr(self.texts, key)
        values = {"support": self.support_contact, **values}
        if isinstance(value, RichText):
            # Підставлені значення (контакт, посилання, дата) — звичайний текст усередині HTML.
            return value.text.format(**{k: html.escape(str(v), quote=False) for k, v in values.items()})
        return value.format(**values)

    def rich(self, key: str | None) -> RichText | None:
        """Розширений текст (HTML + кнопки) для ключа texts.json, або None для звичайного рядка."""
        if not key or key not in Texts.model_fields:
            return None
        value = getattr(self.texts, key)
        return value if isinstance(value, RichText) else None

    def todo_markers(self) -> list[str]:
        found = [
            f"texts.{key}"
            for key, value in self.texts
            if TODO_MARKER in (value.text if isinstance(value, RichText) else value)
        ]
        found += [f"checkin.{q.id}" for q in self.checkin.questions if TODO_MARKER in q.text]
        found += [
            f"instructions.{group}.{day}"
            for group, days in self.instructions.root.items()
            for day, value in days.items()
            if TODO_MARKER in value
        ]
        return found


def _read(path: Path, model: type[BaseModel]) -> Any:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContentError(f"Контент: файл не знайдено: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ContentError(f"Контент: {path.name} — невалідний JSON (рядок {exc.lineno}, колонка {exc.colno}): {exc.msg}") from exc
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '<корінь>'}: {err['msg']}" for err in exc.errors()
        )
        raise ContentError(f"Контент: {path.name} — {details}") from exc


def load_content(content_dir: Path, support_contact: str = "") -> Content:
    return Content(
        instructions=_read(content_dir / "instructions.json", Instructions),
        checkin=_read(content_dir / "checkin.json", Checkin),
        texts=_read(content_dir / "texts.json", Texts),
        support_contact=support_contact,
    )
