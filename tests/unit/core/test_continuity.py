"""The continuity of work: the escalation ladder of a task without activity (US-E7-01)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.unit.core.contract_fixtures import OWNER, PROJECT, change, make_service, note, put
from tests.unit.core.test_gate import contract
from zephyros.core.continuity import (
    ALERT_HUMAN,
    ALERT_MAIN,
    RESTART,
    WAKE,
    Continuity,
    TaskState,
    Timing,
    ready_for_delivery,
)
from zephyros.core.principals import OPERATOR

T0 = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)


def timing(restarts: int = 2) -> Timing:
    return Timing(inactivity_seconds=900, restarts=restarts, unblock_seconds=3600)


def state(side: str = "contractor") -> TaskState:
    return TaskState("p_abcdef12", side, "delivery", "the base is ready", T0)


def test_the_ladder_walks_wake_restarts_main_then_human() -> None:
    continuity = Continuity(timing(restarts=2))
    continuity.expect(state())
    assert continuity.ladder() == [WAKE, RESTART, RESTART, ALERT_MAIN, ALERT_HUMAN]
    # nothing is due before the configured duration
    assert continuity.due(T0 + timedelta(seconds=899)) == []
    # a due step is reported until it is carried out
    (again,) = continuity.due(T0 + timedelta(seconds=900))
    assert again.kind == WAKE
    # the wake, then a restart per configured step, then the main agent
    steps = []
    now = T0
    for _ in range(4):
        now += timedelta(seconds=900)
        (step,) = continuity.due(now)
        steps.append(step.kind)
        continuity.taken(step, now)
    assert steps == [WAKE, RESTART, RESTART, ALERT_MAIN]
    # the human is alerted after the delay the main agent had
    assert continuity.due(now + timedelta(seconds=3599)) == []
    (final,) = continuity.due(now + timedelta(seconds=3600))
    assert final.kind == ALERT_HUMAN
    continuity.taken(final, now + timedelta(seconds=3600))
    # the ladder is walked out: nothing more, the expectation stays until answered
    assert continuity.due(now + timedelta(days=1)) == []
    assert len(list(continuity.waiting())) == 1


def test_each_step_gives_the_instance_the_task_state() -> None:
    continuity = Continuity(timing(restarts=1))
    continuity.expect(state())
    (wake,) = continuity.due(T0 + timedelta(seconds=900))
    assert "p_abcdef12" in wake.prompt
    assert "the base is ready" in wake.prompt
    assert "delivery" in wake.prompt
    assert "contract.read" in wake.prompt  # the hub keeps the state; the instance asks it


def test_an_instance_that_calls_resets_the_current_step() -> None:
    continuity = Continuity(timing())
    continuity.expect(state())
    later = T0 + timedelta(seconds=800)
    continuity.observe("p_abcdef12", "contractor", later)
    assert continuity.due(T0 + timedelta(seconds=900)) == []
    assert len(continuity.due(later + timedelta(seconds=900))) == 1


def test_the_event_that_was_expected_ends_the_ladder() -> None:
    continuity = Continuity(timing())
    continuity.expect(state())
    assert continuity.answered("p_abcdef12", "contractor")
    assert not continuity.answered("p_abcdef12", "contractor")
    assert continuity.due(T0 + timedelta(days=1)) == []


def test_each_side_walks_its_own_ladder() -> None:
    continuity = Continuity(timing())
    continuity.expect(state("contractor"))
    continuity.expect(state("owner"))
    taken = continuity.due(T0 + timedelta(seconds=900))
    assert sorted(step.expectation.side for step in taken) == ["contractor", "owner"]
    assert continuity.answered("p_abcdef12", "owner")
    remaining = continuity.due(T0 + timedelta(seconds=1801))
    # the owner's expectation is answered; the contractor's second step is due
    assert [step.expectation.side for step in remaining] == ["contractor"]


def test_a_held_task_walks_no_ladder_until_it_is_released() -> None:
    """ADR 0021: the base is ready before the contract; the task waits for it, not the side."""
    continuity = Continuity(timing())
    continuity.expect(state(), held=True)
    assert continuity.due(T0 + timedelta(days=1)) == []
    assert len(list(continuity.waiting())) == 1  # held, the task is still kept
    ready = T0 + timedelta(hours=5)
    continuity.release("p_abcdef12", "contractor", ready)
    # the ladder counts from the release, not from the moment the task began
    assert continuity.due(ready + timedelta(seconds=899)) == []
    (step,) = continuity.due(ready + timedelta(seconds=900))
    assert step.kind == WAKE


def test_a_hold_stops_the_ladder_and_a_release_starts_it_again_from_the_wake() -> None:
    continuity = Continuity(timing())
    continuity.expect(state())
    (wake,) = continuity.due(T0 + timedelta(seconds=900))
    continuity.taken(wake, T0 + timedelta(seconds=900))
    continuity.hold("p_abcdef12", "contractor")
    assert continuity.due(T0 + timedelta(days=1)) == []
    # the side's calls while held do not matter: the release sets the start
    continuity.observe("p_abcdef12", "contractor", T0 + timedelta(hours=2))
    released = T0 + timedelta(hours=3)
    continuity.release("p_abcdef12", "contractor", released)
    assert continuity.due(released + timedelta(seconds=899)) == []
    (step,) = continuity.due(released + timedelta(seconds=900))
    assert step.kind == WAKE  # not the restart that came next before the hold


def test_a_release_leaves_a_task_that_was_not_held_on_its_step() -> None:
    continuity = Continuity(timing())
    continuity.expect(state())
    (wake,) = continuity.due(T0 + timedelta(seconds=900))
    continuity.taken(wake, T0 + timedelta(seconds=900))
    continuity.release("p_abcdef12", "contractor", T0 + timedelta(seconds=1000))
    (step,) = continuity.due(T0 + timedelta(seconds=1800))
    assert step.kind == RESTART


def test_a_held_task_is_answered_by_its_event_and_a_side_without_one_is_left_alone() -> None:
    continuity = Continuity(timing())
    continuity.expect(state(), held=True)
    continuity.hold("p_other999", "contractor")
    continuity.release("p_other999", "contractor", T0)
    continuity.release("p_abcdef12", "owner", T0)
    assert [expectation.side for expectation in continuity.waiting()] == ["contractor"]
    assert continuity.due(T0 + timedelta(days=1)) == []
    assert continuity.answered("p_abcdef12", "contractor")
    assert list(continuity.waiting()) == []


def test_the_contract_is_ready_for_delivery_once_it_names_a_test_and_nothing_can_change_it() -> (
    None
):
    service, _, _ = make_service()

    def ready() -> bool:
        return ready_for_delivery(
            service.read(OPERATOR, PROJECT), service.pending(OPERATOR, PROJECT)
        )

    assert not ready()  # no contract yet
    drafted = contract((1, "P1", ("test",), ()))
    drafted["US-E1-01.1"] = {
        "type": "criterion",
        "id": "US-E1-01.1",
        "story": "US-E1-01",
        "text": "c",
    }
    service.apply_reserved(OPERATOR, PROJECT, change(*(put(item) for item in drafted.values())))
    assert not ready()  # a story and its criterion, but no test named yet
    named = {**drafted["US-E1-01.1"], "test": "checks/a.py::test_a"}
    proposed = service.propose(OWNER, PROJECT, change(put(named)))
    assert not ready()  # the test is proposed, not decided
    service.approve(OPERATOR, PROJECT, proposed.id)
    assert ready()
    # two changes of this version await a decision: the contract may change again
    service.propose(OWNER, PROJECT, change(put(note(1, "EXG-01"))))
    newer = service.propose(OWNER, PROJECT, change(put(note(2, "EXG-01"))))
    assert not ready()
    service.approve(OPERATOR, PROJECT, newer.id)
    # the other one was proposed on an older version: it can no longer be approved
    assert len(service.pending(OPERATOR, PROJECT)) == 1
    assert ready()
