"""The continuity of work (US-E7-01; EXG-21, EXG-22, EXG-23, EXG-45, EXG-59).

The hub keeps, per project side, the expectation of a task: a staging area is prepared and a
delivery is expected from the Contractor; a change is proposed and a decision is expected from
the Owner. An expectation is answered by the event it waits for (a delivery declared, a change
decided), and any call from the side counts as activity.

A side whose task stays without activity walks the configured ladder: wake the instance (a turn
that states the task and what the hub keeps), restart it (the same, on a fresh instance), alert
the main agent, then alert the human. Each step is due `continuity.inactivity_seconds` after the
last activity or the previous step; the step after the restarts waits `continuity.unblock_seconds`.

A task the side cannot carry yet is held, for a reason the hub records: it walks no ladder
until it is released, and then starts from the first step (ADR 0021). The Contractor's delivery
is held while the contract is not ready for it (`delivery_waits_for`): the base is prepared
first, for the Owner side to read it, and the Owner side writes the acceptance tests before the
Contractor builds against them.

What the hub keeps of each task — its step on the ladder, when it last counted, why it is held
— goes to a store with every change, and a hub that starts again takes it back: a restart
neither forgets a task nor walks its ladder again from the wake (T12). One lock covers the
tasks; the hub holds it across a read of the contract and the change it makes to a task, so
that two threads cannot interleave the two (`locked`).

The service reads no clock: it is given the time, and returns the steps that are due.
Executing them — the instance commands, the alerts — belongs to the hub.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from zephyros.core.contract import ContractVersion, PendingChange
from zephyros.core.gate import every_acceptance_test

WAKE = "wake"
RESTART = "restart"
ALERT_MAIN = "alert_main"
ALERT_HUMAN = "alert_human"
DELIVERY = "delivery"
"""The Contractor's task: a base was prepared, a delivery is expected."""
DECISION = "decision"
"""A change of the contract awaits a decision."""
HOLD = "hold"
"""Not a step of the ladder: how the log records that a task is held, and why."""
RELEASE = "release"
"""Not a step of the ladder: how the log records that a held task starts its ladder."""

NO_ACCEPTANCE_TEST = "no_acceptance_test"
"""Why a delivery waits: the version in force names no acceptance test that the gate runs."""
PENDING_DECISION = "pending_decision"
"""Why a delivery waits: a change that could still be approved awaits a decision."""


@dataclass(frozen=True, slots=True)
class Timing:
    """The configured ladder."""

    inactivity_seconds: int
    restarts: int
    unblock_seconds: int

    def __post_init__(self) -> None:
        if min(self.inactivity_seconds, self.restarts, self.unblock_seconds) <= 0:
            raise ValueError("the continuity timings are positive")


@dataclass(frozen=True, slots=True)
class TaskState:
    """What the hub keeps of a task, independent of the memory of any instance (EXG-21)."""

    project_id: str
    side: str
    kind: str
    """`delivery` (a base was prepared) or `decision` (a change is pending)."""
    detail: str
    since: datetime


def delivery_waits_for(version: ContractVersion, pending: Iterable[PendingChange]) -> str | None:
    """What keeps the contract from being ready for the Contractor, or None once it is: the
    version in force names at least one acceptance test (the criterion of a story that covers a
    requirement or a constraint), and no change that could still be approved awaits a decision.
    Until then the Owner side is still writing what the Contractor is to build, and a turn on it
    would be spent on a draft. A change proposed on an older version can no longer be approved:
    it changes nothing. A decision comes first, as it may name the tests."""
    if any(change.base_version == version.number for change in pending):
        return PENDING_DECISION
    if not every_acceptance_test(version.objects):
        return NO_ACCEPTANCE_TEST
    return None


@dataclass(frozen=True, slots=True)
class Expectation:
    """A task the hub expects, and where its ladder stands."""

    state: TaskState
    last: datetime
    """When the current step began to count: the last activity, step or release."""
    step: int = 0
    """How many steps of the ladder were taken."""
    held: str | None = None
    """Why the side cannot carry the task yet, if it cannot: no step is due until it is
    released."""

    @property
    def key(self) -> tuple[str, str]:
        """A project expects at most one task of each kind."""
        return (self.state.project_id, self.state.kind)


class ContinuityStore(Protocol):
    """Where the expectations are kept, so that a restart of the hub finds them."""

    def put(self, expectation: Expectation) -> None:
        """Keep `expectation`, in place of the one of the same project and kind."""
        ...

    def remove(self, project_id: str, kind: str) -> None: ...

    def all(self) -> list[Expectation]: ...


@dataclass(frozen=True, slots=True)
class Step:
    """One step the hub must take, with the task state to give the instance."""

    kind: str
    expectation: TaskState
    prompt: str


class Continuity:
    def __init__(self, timing: Timing, store: ContinuityStore | None = None) -> None:
        self._timing = timing
        self._store = store
        self._lock = threading.RLock()
        found = store.all() if store is not None else []
        self._waiting: dict[tuple[str, str], Expectation] = {e.key: e for e in found}

    def locked(self) -> AbstractContextManager[bool]:
        """Hold the tasks for a sequence of the caller: read what decides a task, then change
        it. The methods below take the same lock, so they may be called inside."""
        return self._lock

    def _keep(self, expectation: Expectation) -> None:
        self._waiting[expectation.key] = expectation
        if self._store is not None:
            self._store.put(expectation)

    def expect(self, state: TaskState, *, held: str | None = None) -> None:
        """A task the hub now expects a side to carry, in place of any other of its kind in the
        project; one `held` for a reason waits for `release`."""
        with self._lock:
            self._keep(Expectation(state, last=state.since, held=held))

    def hold(self, project_id: str, kind: str, why: str) -> bool:
        """The side cannot carry the task for now, because of `why`: no step is due until
        `release`. True when this changes what the hub keeps: the task was carried, or held for
        another reason."""
        with self._lock:
            expectation = self._waiting.get((project_id, kind))
            if expectation is None or expectation.held == why:
                return False
            self._keep(replace(expectation, held=why))
            return True

    def release(self, project_id: str, kind: str, at: datetime) -> bool:
        """The side can carry the task from `at`. A held task starts its ladder again, from the
        first step, and True says so. A task that was not held keeps its place on the ladder."""
        with self._lock:
            expectation = self._waiting.get((project_id, kind))
            if expectation is None or expectation.held is None:
                return False
            self._keep(replace(expectation, held=None, step=0, last=at))
            return True

    def answered(self, project_id: str, kind: str) -> bool:
        """The event the task waited for arrived."""
        with self._lock:
            if self._waiting.pop((project_id, kind), None) is None:
                return False
            if self._store is not None:
                self._store.remove(project_id, kind)
            return True

    def observe(self, project_id: str, side: str, at: datetime) -> None:
        """A call of the side: the current step of each task it carries counts from now."""
        with self._lock:
            for kind in (DELIVERY, DECISION):
                expectation = self._waiting.get((project_id, kind))
                if expectation is not None and expectation.state.side == side:
                    self._keep(replace(expectation, last=at))

    def waiting(self) -> Iterable[TaskState]:
        with self._lock:
            return [expectation.state for expectation in self._waiting.values()]

    def ladder(self) -> list[str]:
        """The steps, in order: wake, the configured restarts, the main agent, the human."""
        return [WAKE, *([RESTART] * self._timing.restarts), ALERT_MAIN, ALERT_HUMAN]

    def due(self, now: datetime) -> list[Step]:
        """The steps that are due, in ladder order; each waiting task is asked at most once.
        Reporting does not advance the ladder: `taken` does, once the step was carried out."""
        steps: list[Step] = []
        ladder = self.ladder()
        with self._lock:
            for key in sorted(self._waiting):
                expectation = self._waiting[key]
                if expectation.held is not None:
                    continue  # nothing to wake the side for yet
                if expectation.step >= len(ladder):
                    continue  # the ladder is walked out; the expectation stays until answered
                kind = ladder[expectation.step]
                interval = (
                    self._timing.unblock_seconds
                    if kind == ALERT_HUMAN
                    else self._timing.inactivity_seconds
                )
                if (now - expectation.last).total_seconds() >= interval:
                    steps.append(Step(kind, expectation.state, _prompt(kind, expectation.state)))
        return steps

    def taken(self, step: Step, at: datetime) -> None:
        """The step was carried out: the ladder advances and the next one waits from `at`.
        A step that could not be carried out (a busy instance) is left where it is."""
        with self._lock:
            expectation = self._waiting.get((step.expectation.project_id, step.expectation.kind))
            if expectation is not None and expectation.state == step.expectation:
                self._keep(replace(expectation, step=expectation.step + 1, last=at))


def _prompt(kind: str, state: TaskState) -> str:
    where = f"{state.project_id}/{state.side}"
    if kind == WAKE:
        lead = "The hub sees no activity on your task and wakes you."
    elif kind == RESTART:
        lead = "The hub restarts your instance: the task is still without activity."
    else:
        lead = "The hub reports the task."
    return (
        f"{lead} Task: {state.kind} ({state.detail}) for {where}. "
        f"The hub keeps the state; ask it (contract.read, delivery.status) and carry on."
    )
