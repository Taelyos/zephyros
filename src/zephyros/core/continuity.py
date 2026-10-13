"""The continuity of work (US-E7-01; EXG-21, EXG-22, EXG-23, EXG-45, EXG-59).

The hub keeps, per project side, the expectation of a task: a staging area is prepared and a
delivery is expected from the Contractor; a change is proposed and a decision is expected from
the Owner. An expectation is answered by the event it waits for (a delivery declared, a change
decided), and any call from the side counts as activity.

A side whose task stays without activity walks the configured ladder: wake the instance (a turn
that states the task and what the hub keeps), restart it (the same, on a fresh instance), alert
the main agent, then alert the human. Each step is due `continuity.inactivity_seconds` after the
last activity or the previous step; the step after the restarts waits `continuity.unblock_seconds`.

A task the side cannot carry yet is held: it walks no ladder until it is released, and then
starts from the first step (ADR 0021). The Contractor's delivery is held while the contract is
not ready for it (`ready_for_delivery`): the base is prepared first, for the Owner side to read
it, and the Owner side writes the acceptance tests before the Contractor builds against them.

The service is pure: it reads a clock and returns the steps that are due. Executing them — the
instance commands, the alerts — belongs to the hub.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from zephyros.core.contract import ContractVersion, PendingChange
from zephyros.core.gate import every_acceptance_test

WAKE = "wake"
RESTART = "restart"
ALERT_MAIN = "alert_main"
ALERT_HUMAN = "alert_human"


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


def ready_for_delivery(version: ContractVersion, pending: Iterable[PendingChange]) -> bool:
    """Whether the contract is ready for the Contractor: the version in force names at least
    one acceptance test (the criterion of a story that covers a requirement or a constraint),
    and no change that could still be approved awaits a decision. Until then the Owner side is
    still writing what the Contractor is to build, and a turn on it would be spent on a draft.
    A change proposed on an older version can no longer be approved: it changes nothing."""
    if any(change.base_version == version.number for change in pending):
        return False
    return bool(every_acceptance_test(version.objects))


@dataclass(slots=True)
class Expectation:
    state: TaskState
    step: int = 0
    """How many steps of the ladder were taken."""
    last: datetime = field(default=None)  # type: ignore[assignment]  # reason: set on creation
    held: bool = False
    """The side cannot carry the task yet: no step is due until it is released."""


@dataclass(frozen=True, slots=True)
class Step:
    """One step the hub must take, with the task state to give the instance."""

    kind: str
    expectation: TaskState
    prompt: str


class Continuity:
    def __init__(self, timing: Timing) -> None:
        self._timing = timing
        self._waiting: dict[tuple[str, str], Expectation] = {}

    def expect(self, state: TaskState, *, held: bool = False) -> None:
        """A task the hub now expects a side to carry; a held one waits for `release`."""
        key = (state.project_id, state.side)
        self._waiting[key] = Expectation(state, last=state.since, held=held)

    def hold(self, project_id: str, side: str) -> None:
        """The side cannot carry its task for now: no step is due until `release`."""
        expectation = self._waiting.get((project_id, side))
        if expectation is not None:
            expectation.held = True

    def release(self, project_id: str, side: str, at: datetime) -> None:
        """The side can carry its task from `at`. A held task starts its ladder again, from the
        first step: what the side is to do changed while it was held. A task that was not held
        keeps its place on the ladder."""
        expectation = self._waiting.get((project_id, side))
        if expectation is not None and expectation.held:
            expectation.held = False
            expectation.step = 0
            expectation.last = at

    def answered(self, project_id: str, side: str) -> bool:
        """The event the side was expected to produce arrived."""
        return self._waiting.pop((project_id, side), None) is not None

    def observe(self, project_id: str, side: str, at: datetime) -> None:
        """A call of the side: the current step counts its waiting from now."""
        expectation = self._waiting.get((project_id, side))
        if expectation is not None:
            expectation.last = at

    def waiting(self) -> Iterable[TaskState]:
        return [expectation.state for expectation in self._waiting.values()]

    def ladder(self) -> list[str]:
        """The steps, in order: wake, the configured restarts, the main agent, the human."""
        return [WAKE, *([RESTART] * self._timing.restarts), ALERT_MAIN, ALERT_HUMAN]

    def due(self, now: datetime) -> list[Step]:
        """The steps that are due, in ladder order; each waiting side is asked at most once.
        Reporting does not advance the ladder: `taken` does, once the step was carried out."""
        steps: list[Step] = []
        ladder = self.ladder()
        for key in sorted(self._waiting):
            expectation = self._waiting[key]
            if expectation.held:
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
        expectation = self._waiting.get((step.expectation.project_id, step.expectation.side))
        if expectation is not None:
            expectation.step += 1
            expectation.last = at


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
