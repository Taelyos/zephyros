"""The notifications of the hub: queued by whoever raises them, sent by their own worker, and
each one recorded once, whatever came of it (EXG-43, EXG-48; T03)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping

import httpx
import pytest

from zephyros.adapters.memory.clock import FakeClock
from zephyros.adapters.memory.log_store import InMemoryLogStore
from zephyros.core.audit import Action, AuditLog, AuditRecord, Outcome, Reason
from zephyros.core.notifications import Notifier
from zephyros.core.webhooks import NotifyResult

PROJECT = "p_abcdef12"


class Receiver:
    """A send function that records what it was given and answers as told."""

    def __init__(self, answer: NotifyResult | Exception | None = None) -> None:
        self.answer = answer if answer is not None else NotifyResult(status=204)
        self.sent: list[tuple[str, dict[str, object]]] = []

    def __call__(self, event: str, payload: Mapping[str, object]) -> NotifyResult:
        self.sent.append((event, dict(payload)))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def make(receiver: Receiver, limit: int = 10) -> tuple[Notifier, AuditLog]:
    audit = AuditLog(InMemoryLogStore(), FakeClock())
    return Notifier(audit, receiver, limit=limit), audit


def notify(notifier: Notifier, event: str = "delivery.decided", n: int = 1) -> None:
    notifier.notify(
        event,
        {"project_id": PROJECT, "delivery_id": f"d{n}", "decision": "PASS"},
        stream=PROJECT,
        record={"event": event, "delivery_id": f"d{n}"},
    )


def notifications(audit: AuditLog) -> list[AuditRecord]:
    return [record for record in audit.read(PROJECT) if record.action is Action.NOTIFICATION]


def drain(notifier: Notifier) -> int:
    sent = 0
    while notifier.step():
        sent += 1
    return sent


def test_an_event_is_queued_then_sent_in_order_and_recorded_once() -> None:
    receiver = Receiver()
    notifier, audit = make(receiver)
    notify(notifier, n=1)
    notify(notifier, "delivery.reviewed", n=2)
    assert receiver.sent == []  # the caller does not wait for the receiver
    assert audit.read(PROJECT) == []
    assert drain(notifier) == 2
    assert [(event, payload["delivery_id"]) for event, payload in receiver.sent] == [
        ("delivery.decided", "d1"),
        ("delivery.reviewed", "d2"),
    ]
    records = audit.read(PROJECT)
    assert [(r.action, r.outcome, r.principal) for r in records] == [
        (Action.NOTIFICATION, Outcome.OK, "hub"),
        (Action.NOTIFICATION, Outcome.OK, "hub"),
    ]
    assert json.loads(records[1].parameters) == {"event": "delivery.reviewed", "delivery_id": "d2"}
    assert not notifier.step()


def test_an_error_status_is_recorded_as_an_error() -> None:
    notifier, audit = make(Receiver(NotifyResult(status=503)))
    notify(notifier)
    drain(notifier)
    (record,) = notifications(audit)
    assert record.outcome is Outcome.ERROR
    assert json.loads(record.parameters)["error"] == "status 503"


def test_a_refused_host_is_recorded_with_the_egress_refusal() -> None:
    """EXG-48: the notification never left; the log says which host, and why."""
    notifier, audit = make(Receiver(NotifyResult(status=302, refused="elsewhere.example")))
    notify(notifier)
    drain(notifier)
    egress, sent = audit.read(PROJECT)
    assert (egress.action, egress.outcome, egress.reason) == (
        Action.EGRESS,
        Outcome.REFUSED,
        Reason.EGRESS,
    )
    assert json.loads(egress.parameters) == {"host": "elsewhere.example", "purpose": "notification"}
    assert (sent.action, sent.outcome) == (Action.NOTIFICATION, Outcome.ERROR)
    assert json.loads(sent.parameters)["error"] == Reason.EGRESS.value


def test_a_receiver_out_of_reach_is_an_error_named_without_its_message() -> None:
    """The message of a transport error may carry the url and its secrets: the log keeps the
    kind of failure only, and the worker goes on with the next notification."""
    secret_url = "https://hooks.example.invalid/?token=s3cr3t"  # pragma: allowlist secret
    notifier, audit = make(Receiver(httpx.ConnectError(f"cannot reach {secret_url}")))
    notify(notifier, n=1)
    notify(notifier, n=2)
    assert drain(notifier) == 2
    first, second = notifications(audit)
    assert first.outcome is Outcome.ERROR
    assert json.loads(first.parameters)["error"] == "ConnectError"
    assert "s3cr3t" not in first.parameters
    assert second.outcome is Outcome.ERROR


def test_beyond_its_limit_the_queue_records_an_event_as_not_sent() -> None:
    receiver = Receiver()
    notifier, audit = make(receiver, limit=2)
    for n in (1, 2, 3):
        notify(notifier, n=n)
    (full,) = notifications(audit)  # at once, on the caller's thread
    assert full.outcome is Outcome.ERROR
    assert json.loads(full.parameters) == {
        "event": "delivery.decided",
        "delivery_id": "d3",
        "error": "queue_full",
    }
    assert drain(notifier) == 2
    assert [payload["delivery_id"] for _event, payload in receiver.sent] == ["d1", "d2"]


def test_what_was_not_sent_when_the_hub_stops_is_recorded_as_such() -> None:
    receiver = Receiver()
    notifier, audit = make(receiver)
    notify(notifier, n=1)
    notify(notifier, n=2)
    notifier.shutdown()
    assert receiver.sent == []
    assert [json.loads(r.parameters)["error"] for r in notifications(audit)] == [
        "hub_stopped",
        "hub_stopped",
    ]
    assert not notifier.step()


@pytest.mark.parametrize("queued", [True, False])
def test_the_worker_waits_only_while_nothing_is_queued(queued: bool) -> None:
    notifier, _audit = make(Receiver())
    if queued:
        notify(notifier)
    started = time.monotonic()
    notifier.wait(0.2 if not queued else 10)
    elapsed = time.monotonic() - started
    assert elapsed < 1 if queued else elapsed >= 0.2


def test_a_notification_wakes_the_worker_that_waits() -> None:
    """The worker sleeps in `wait` between notifications; one queued meanwhile goes out at once,
    not at the end of the wait."""
    notifier, _audit = make(Receiver())
    waited: list[float] = []

    def worker() -> None:
        started = time.monotonic()
        notifier.wait(10)
        waited.append(time.monotonic() - started)

    thread = threading.Thread(target=worker)
    thread.start()
    time.sleep(0.3)  # the worker is in its wait by now
    notify(notifier)
    thread.join(15)
    assert waited
    assert waited[0] < 5
