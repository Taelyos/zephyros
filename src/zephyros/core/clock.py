"""Time, as the hub sees it. Expiry, revocation and the log all read time from a Clock only,
never from a request, so that an agent cannot move them (ATK-87)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """The current time, timezone-aware, in UTC."""
        ...


def format_time(moment: datetime) -> str:
    """ISO 8601 in UTC with microseconds and a final Z: the form that records carry."""
    if moment.tzinfo is None:
        raise ValueError("a time without a timezone is ambiguous")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_time(text: str) -> datetime:
    """The inverse of format_time."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


class SystemClock:
    """The clock of the machine the hub runs on."""

    def now(self) -> datetime:
        return datetime.now(UTC)
