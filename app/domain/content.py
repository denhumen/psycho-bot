"""Завантаження й валідація content/*.json. Невалідний контент — застосунок не стартує."""

import json
import string
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


class Texts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    welcome_ask_code: NonEmpty
    not_registered: NonEmpty
    code_invalid_format: NonEmpty
    code_not_found: NonEmpty
    code_conflict: NonEmpty
    consent: NonEmpty
    btn_consent_yes: NonEmpty
    btn_consent_no: NonEmpty
    consent_declined: NonEmpty
    choose_time: NonEmpty
    time_explanation: NonEmpty
    btn_ready: NonEmpty
    started: NonEmpty
    btn_checkin: NonEmpty
    checkin_thanks: NonEmpty
    checkin_expired: NonEmpty
    checkin_already_done: NonEmpty
    auto_reminder: NonEmpty
    final_message: NonEmpty
    help: NonEmpty
    stop_confirm: NonEmpty
    btn_stop_yes: NonEmpty
    btn_stop_no: NonEmpty
    stopped: NonEmpty
    stop_cancelled: NonEmpty
    stop_unavailable: NonEmpty
    time_choose_new: NonEmpty
    time_changed: NonEmpty
    time_unavailable: NonEmpty
    free_text_ack: NonEmpty
    free_text_ack_active: NonEmpty
    media_not_supported: NonEmpty
    status_registered: NonEmpty
    status_consented: NonEmpty
    status_ready: NonEmpty
    status_waiting: NonEmpty
    status_active: NonEmpty
    status_today_done: NonEmpty
    status_today_pending: NonEmpty
    status_today_missed: NonEmpty
    status_completed: NonEmpty
    status_declined: NonEmpty
    status_withdrawn: NonEmpty
    button_outdated: NonEmpty

    @model_validator(mode="after")
    def _placeholders(self) -> "Texts":
        errors = []
        for key, value in self:
            try:
                used = {field for _, field, _, _ in string.Formatter().parse(value) if field is not None}
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
        template: str = getattr(self.texts, key)
        return template.format(support=self.support_contact, **values)

    def todo_markers(self) -> list[str]:
        found = [f"texts.{key}" for key, value in self.texts if TODO_MARKER in value]
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
