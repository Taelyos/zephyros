"""A cap on the refusals of one instance (ATK-71).

An instance that generates a flood of refused actions is answered without doing the work once
its refusals in the last minute reach the configured number. Every refusal — the flood and the
throttle's own — stays in the log; the window slides, so an instance that calms down is served
again. The budget is kept in memory: a restart forgets it, which is what a rate limit wants.
"""

from __future__ import annotations

from collections import deque

WINDOW_SECONDS = 60.0


class RefusalBudget:
    """The sliding-window count of one instance's refusals."""

    def __init__(self, per_minute: int) -> None:
        if per_minute <= 0:
            raise ValueError("the budget is positive")
        self._per_minute = per_minute
        self._refusals: dict[str, deque[float]] = {}

    def refused(self, key: str, at: float) -> None:
        """Record one refusal of `key` at `at` (a monotonic second)."""
        recent = self._refusals.setdefault(key, deque())
        recent.append(at)
        self._slide(recent, at)

    def over(self, key: str, at: float) -> bool:
        """True when `key` reached the cap within the last minute."""
        recent = self._refusals.get(key)
        if recent is None:
            return False
        self._slide(recent, at)
        return len(recent) >= self._per_minute

    @staticmethod
    def _slide(recent: deque[float], at: float) -> None:
        cutoff = at - WINDOW_SECONDS
        while recent and recent[0] < cutoff:
            recent.popleft()
