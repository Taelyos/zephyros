"""The reaper: a turn must not outlive the hub past its limit (build log, decision 22).

The hub stops a turn that runs too long (core/lifecycle.py), but the hub itself can be gone:
nothing then stops a turn it had started. The filter outlives the hub and is the other
component that reaches the runtime, so it sweeps the turns it can see — the containers
labelled ``org.zephyros.role=turn`` — and stops any whose container is older than the longest
turn of the configuration. A turn younger than the limit is left alone, so a live hub keeps
deciding the outcome of its own turns, and the sweeps pass through the same typed operations
as the hub's, which puts them in the filter's journal the same way.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from zephyros.adapters.filter.executor import Executor
from zephyros.core.broker import BrokerError, Op, Request
from zephyros.core.containers import Role
from zephyros.core.ports.agent_engine import Side

DEFAULT_INTERVAL_SECONDS = 30


def _started_at(text: object) -> datetime | None:
    """The start time of a container, from what the runtime reports. None when unreadable."""
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


class Reaper:
    """Stops the turns past the configured limit. One sweep is synchronous, so that the
    tests drive it without a loop; `run` repeats it until the stop event is set.

    `grace_seconds` delays the reap past the limit: the hub's own watchdog acts within one
    poll of the limit, so the caller gives a few polls of grace and a live hub always
    decides the fate of its own turns first. The reaper is for the hub that is gone."""

    def __init__(
        self,
        executor: Executor,
        max_age_seconds: int,
        *,
        grace_seconds: int = 0,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._executor = executor
        self._max_age = max_age_seconds + grace_seconds
        self._interval = interval_seconds
        self._now = now or (lambda: datetime.now(UTC))

    def sweep(self) -> list[str]:
        """Stop every turn older than the limit. Returns what it stopped, as `project/side`."""
        try:
            listed = self._executor.perform(Request(Op.LIST))
        except BrokerError:
            return []
        containers = listed.get("containers")
        if not isinstance(containers, list):
            return []
        now = self._now()
        stopped: list[str] = []
        for item in containers:
            if item.get("role") != Role.TURN.value:
                continue
            started = _started_at(item.get("started_at"))
            if started is None or (now - started).total_seconds() <= self._max_age:
                continue
            project_id, side = str(item.get("project_id") or ""), str(item.get("side") or "")
            if not project_id or side not in {value.value for value in Side}:
                continue
            try:
                self._executor.perform(Request(Op.TURN_STOP, project_id, Side(side)))
            except BrokerError:
                continue
            stopped.append(f"{project_id}/{side}")
        return stopped

    async def run(self, stop: asyncio.Event) -> None:
        """Sweep now, then on the interval, until `stop` is set."""
        while not stop.is_set():
            await asyncio.to_thread(self.sweep)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue
