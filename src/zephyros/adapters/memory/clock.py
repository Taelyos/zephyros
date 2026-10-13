"""A clock that only moves when told to."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
        if self._now.tzinfo is None:
            raise ValueError("a clock needs a timezone-aware time")

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("a clock needs a timezone-aware time")
        self._now = moment
