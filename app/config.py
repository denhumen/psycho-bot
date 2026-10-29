import re
from datetime import time
from functools import cached_property
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def parse_hhmm(value: str) -> time:
    value = value.strip()
    if not _HHMM.match(value):
        raise ValueError(f"очікується час у форматі HH:MM, отримано {value!r}")
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr = SecretStr("")
    database_path: Path = Path("/data/study.db")
    session_secret: SecretStr = SecretStr("")
    port: int = 8000
    timezone: str = "Europe/Kyiv"

    code_validation: Literal["whitelist", "open"] = "whitelist"
    code_regex: str = r"^[A-Za-zА-Яа-яІіЇїЄєҐґ0-9_-]{4,32}$"
    reminder_time_options: str = "09:00,12:00,15:00,18:00,20:00,21:00"
    start_offset_days: int = 1
    auto_reminder_after_hours: float = 3
    post_survey_url_template: str = ""

    admin_chat_id: int | None = None
    backup_chat_id: int | None = None
    backup_time: str = "03:00"
    digest_time: str = "09:00"
    support_contact: str = "@study_support"

    allocation_block_size: int = 4
    allocation_initial_blocks: int = 100

    cookie_secure: bool = True
    dev_day_minutes: int | None = None
    content_dir: Path = PROJECT_ROOT / "content"
    log_level: str = "INFO"

    @field_validator("admin_chat_id", "backup_chat_id", "dev_day_minutes", mode="before")
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("backup_time", "digest_time")
    @classmethod
    def _check_hhmm(cls, value: str) -> str:
        parse_hhmm(value)
        return value.strip()

    @field_validator("reminder_time_options")
    @classmethod
    def _check_options(cls, value: str) -> str:
        options = [item.strip() for item in value.split(",") if item.strip()]
        if not options:
            raise ValueError("REMINDER_TIME_OPTIONS не може бути порожнім")
        for item in options:
            parse_hhmm(item)
        return ",".join(options)

    @field_validator("code_regex")
    @classmethod
    def _check_regex(cls, value: str) -> str:
        re.compile(value)
        return value

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"невідома часова зона {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _check_numbers(self) -> "Settings":
        if self.allocation_block_size < 2 or self.allocation_block_size % 2:
            raise ValueError("ALLOCATION_BLOCK_SIZE має бути парним і ≥ 2")
        if self.allocation_initial_blocks < 1:
            raise ValueError("ALLOCATION_INITIAL_BLOCKS має бути ≥ 1")
        if self.start_offset_days < 0:
            raise ValueError("START_OFFSET_DAYS має бути ≥ 0")
        if self.auto_reminder_after_hours < 0:
            raise ValueError("AUTO_REMINDER_AFTER_HOURS має бути ≥ 0")
        if self.dev_day_minutes is not None and self.dev_day_minutes < 1:
            raise ValueError("DEV_DAY_MINUTES має бути ≥ 1 або порожнім")
        if self.post_survey_url_template and "{code}" not in self.post_survey_url_template:
            raise ValueError("POST_SURVEY_URL_TEMPLATE має містити {code}")
        return self

    @cached_property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @cached_property
    def reminder_times(self) -> list[str]:
        return self.reminder_time_options.split(",")

    @cached_property
    def code_pattern(self) -> re.Pattern[str]:
        return re.compile(self.code_regex)

    def check_runtime(self) -> None:
        """Вимоги для повноцінного запуску (бот + адмінка), не потрібні тестам і CLI."""
        missing = [
            name
            for name, value in (
                ("BOT_TOKEN", self.bot_token.get_secret_value()),
                ("SESSION_SECRET", self.session_secret.get_secret_value()),
                ("POST_SURVEY_URL_TEMPLATE", self.post_survey_url_template),
            )
            if not value
        ]
        if missing:
            raise RuntimeError("Не задано обов'язкові змінні оточення: " + ", ".join(missing))
        if len(self.session_secret.get_secret_value()) < 32:
            raise RuntimeError("SESSION_SECRET має бути щонайменше 32 символи")
