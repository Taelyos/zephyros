"""The notifications of the hub to the main agent (EXG-43, EXG-48; US-E7-01).

Whoever raises an event — a handler on a listener, the delivery loop, the watchdog — queues it
and goes on. The notifier's own worker sends the queued events one after the other, so a
receiver that is slow to answer holds up nothing but the notifications behind it: never a
listener of the hub, never a verification, never the watchdog.

Each notification ends in exactly one `notification.sent` record of the log: `ok`, or `error`
with what went wrong — the status the receiver answered, the host the egress policy refused
(with its `egress.refused` record), a failure to reach the receiver, a full queue, or a hub that
stopped before it was sent.

Nothing here starts a thread: the adapter that runs the worker decides how often to call `step`.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from zephyros.core.audit import Action, AuditEvent, AuditLog, Outcome, Reason
from zephyros.core.webhooks import NotifyResult

SYSTEM_PRINCIPAL = "hub"
QUEUE_LIMIT = 1000
"""Notifications waiting to be sent, at most. Beyond, an event is recorded as not sent: an
instance that raises events faster than the receiver answers cannot grow the hub's memory."""

Send = Callable[[str, Mapping[str, object]], NotifyResult]
"""Send one event with its payload; say what came of it."""


@dataclass(frozen=True, slots=True)
class _Queued:
    event: str
    payload: Mapping[str, object] = field(repr=False)
    stream: str
    record: Mapping[str, object] = field(repr=False)


class Notifier:
    """The queue of notifications and the worker's side of it."""

    def __init__(
        self, audit: AuditLog, send: Send | None = None, *, limit: int = QUEUE_LIMIT
    ) -> None:
        self._audit = audit
        self._send = send or (lambda _event, _payload: NotifyResult())
        self._limit = limit
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._queue: deque[_Queued] = deque()

    def notify(
        self,
        event: str,
        payload: Mapping[str, object],
        *,
        stream: str,
        record: Mapping[str, object],
    ) -> None:
        """Queue `event` with the `payload` the receiver gets; `record` is what the log keeps of
        it, in `stream`. Returns at once."""
        queued = _Queued(event, dict(payload), stream, dict(record))
        with self._wake:
            full = len(self._queue) >= self._limit
            if not full:
                self._queue.append(queued)
                self._wake.notify()
        if full:
            self._record(queued, NotifyResult(), error="queue_full")

    # ------------------------------------------------------------------ the worker
    def step(self) -> bool:
        """Send the next queued notification and record what came of it. False when there was
        none."""
        with self._lock:
            if not self._queue:
                return False
            queued = self._queue.popleft()
        try:
            result = self._send(queued.event, queued.payload)
        except Exception as error:  # reason: a receiver that cannot be reached is an outcome
            # The name of the error only: its message may carry the url or a secret.
            self._record(queued, NotifyResult(), error=type(error).__name__)
        else:
            self._record(queued, result)
        return True

    def tick(self) -> None:
        """Nothing to watch between notifications."""

    def wait(self, timeout: float) -> None:
        """Sleep until a notification is queued, or `timeout` passes."""
        with self._wake:
            if not self._queue:
                self._wake.wait(timeout)

    def wake(self) -> None:
        """Let a thread that is in `wait` go on."""
        with self._wake:
            self._wake.notify_all()

    def shutdown(self) -> None:
        """The hub is stopping: what was not sent is recorded as such, never lost silently."""
        with self._lock:
            left = list(self._queue)
            self._queue.clear()
        for queued in left:
            self._record(queued, NotifyResult(), error="hub_stopped")

    # ------------------------------------------------------------------ the record
    def _record(self, queued: _Queued, result: NotifyResult, *, error: str = "") -> None:
        """The one record of a notification: sent, answered by an error status, refused before
        it left (EXG-48), or not sent at all."""
        if result.refused:
            self._append(
                queued.stream,
                Action.EGRESS,
                Outcome.REFUSED,
                {"host": result.refused, "purpose": "notification"},
                Reason.EGRESS,
            )
            error = Reason.EGRESS.value
        elif result.status is not None and not 200 <= result.status < 300:
            error = f"status {result.status}"
        if error:
            self._append(
                queued.stream, Action.NOTIFICATION, Outcome.ERROR, {**queued.record, "error": error}
            )
        else:
            self._append(queued.stream, Action.NOTIFICATION, Outcome.OK, queued.record)

    def _append(
        self,
        stream: str,
        action: Action,
        outcome: Outcome,
        parameters: Mapping[str, object],
        reason: Reason = Reason.NONE,
    ) -> None:
        self._audit.append(
            AuditEvent(
                stream=stream,
                principal=SYSTEM_PRINCIPAL,
                action=action,
                outcome=outcome,
                reason=reason,
                parameters=parameters,
            )
        )
