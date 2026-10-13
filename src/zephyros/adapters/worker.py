"""The thread that runs the instance manager: the jobs one after the other, the watchdog on a beat.

All calls to the container filter happen on this thread, so a slow filter never holds up the
listeners of the hub, and two jobs never overlap. A bug in one round is reported by name only
(never a message, which could hold a secret) and the next round goes on.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from typing import Protocol

BACKOFF_SECONDS = 1.0


class Steppable(Protocol):
    """A service that does its jobs one at a time and watches between them."""

    def step(self) -> bool: ...

    def tick(self) -> None: ...

    def wait(self, timeout: float) -> None: ...

    def wake(self) -> None: ...

    def shutdown(self) -> None: ...


class InstanceWorker:
    """Runs a service: the instance manager, or the delivery loop."""

    def __init__(
        self,
        manager: Steppable,
        poll_seconds: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        report: Callable[[str], None] | None = None,
    ) -> None:
        self._manager = manager
        self._poll = poll_seconds
        self._monotonic = monotonic
        self._report = report or (lambda text: sys.stderr.write(text + "\n"))
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, name="instances", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        """Stop the thread, then end the turns that are running: none outlives the hub."""
        self._stopping.set()
        self._manager.wake()
        self._thread.join(timeout)
        self._manager.shutdown()

    def _run(self) -> None:
        next_tick = self._monotonic()
        while not self._stopping.is_set():
            try:
                worked = self._manager.step()
                now = self._monotonic()
                if now >= next_tick:
                    self._manager.tick()
                    next_tick = self._monotonic() + self._poll
                    now = self._monotonic()
                if not worked and not self._stopping.is_set():
                    self._manager.wait(max(0.0, min(self._poll, next_tick - now)))
            except Exception as error:  # reason: the worker must outlive a bug
                self._report(f"the instance worker met {type(error).__name__}; going on")
                self._stopping.wait(BACKOFF_SECONDS)
