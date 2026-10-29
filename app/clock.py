"""Єдине джерело поточного часу. Тести заморожують і зсувають його."""

from datetime import UTC, datetime, timedelta


class Clock:
    def __init__(self) -> None:
        self._frozen: datetime | None = None

    def now(self) -> datetime:
        if self._frozen is not None:
            return self._frozen
        return datetime.now(UTC)

    def freeze(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("clock.freeze потребує datetime з tz")
        self._frozen = at.astimezone(UTC)

    def advance(self, delta: timedelta) -> None:
        if self._frozen is None:
            raise RuntimeError("clock.advance працює лише із замороженим часом")
        self._frozen += delta

    def reset(self) -> None:
        self._frozen = None


clock = Clock()
