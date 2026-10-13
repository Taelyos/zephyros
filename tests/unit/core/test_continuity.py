"""The continuity of work: the escalation ladder of a task without activity (US-E7-01)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from zephyros.core.continuity import (
    ALERT_HUMAN,
    ALERT_MAIN,
    RESTART,
    WAKE,
    Continuity,
    TaskState,
    Timing,
)

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
