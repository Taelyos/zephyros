"""The continuity of work: the escalation ladder of a task without activity (US-E7-01)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.unit.core.contract_fixtures import (
    OWNER,
    PROJECT,
    change,
    make_service,
    note,
    put,
    story,
)
from tests.unit.core.test_gate import contract
from zephyros.adapters.memory.continuity_store import InMemoryContinuityStore
from zephyros.core.continuity import (
    ALERT_HUMAN,
    ALERT_MAIN,
    ANSWER_TASKS,
    DECISION,
    DELIVERY,
    MAIN_AGENT,
    NO_ACCEPTANCE_TEST,
    OBSTACLE,
    PENDING_DECISION,
    RAISED,
    RESTART,
    WAKE,
    Continuity,
    TaskState,
    Timing,
    delivery_waits_for,
)
from zephyros.core.principals import OPERATOR

T0 = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)


def timing(restarts: int = 2) -> Timing:
    return Timing(inactivity_seconds=900, restarts=restarts, unblock_seconds=3600)


def state(side: str = "contractor") -> TaskState:
    """The Contractor's delivery, or the Owner side's decision."""
    if side == "owner":
        return TaskState("p_abcdef12", side, DECISION, "a change awaits a decision", T0)
    return TaskState("p_abcdef12", side, DELIVERY, "the base is ready", T0)


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
    assert continuity.answered("p_abcdef12", DELIVERY)
    assert not continuity.answered("p_abcdef12", DELIVERY)
    assert continuity.due(T0 + timedelta(days=1)) == []


def test_each_side_walks_its_own_ladder() -> None:
    continuity = Continuity(timing())
    continuity.expect(state("contractor"))
    continuity.expect(state("owner"))
    taken = continuity.due(T0 + timedelta(seconds=900))
    assert sorted(step.expectation.side for step in taken) == ["contractor", "owner"]
    assert continuity.answered("p_abcdef12", DECISION)
    remaining = continuity.due(T0 + timedelta(seconds=1801))
    # the owner's expectation is answered; the contractor's second step is due
    assert [step.expectation.side for step in remaining] == ["contractor"]


def test_a_held_task_walks_no_ladder_until_it_is_released() -> None:
    """ADR 0021: the base is ready before the contract; the task waits for it, not the side."""
    continuity = Continuity(timing())
    continuity.expect(state(), held=NO_ACCEPTANCE_TEST)
    assert continuity.due(T0 + timedelta(days=1)) == []
    assert len(list(continuity.waiting())) == 1  # held, the task is still kept
    ready = T0 + timedelta(hours=5)
    assert continuity.release("p_abcdef12", DELIVERY, ready)
    # the ladder counts from the release, not from the moment the task began
    assert continuity.due(ready + timedelta(seconds=899)) == []
    (step,) = continuity.due(ready + timedelta(seconds=900))
    assert step.kind == WAKE


def test_a_hold_stops_the_ladder_and_a_release_starts_it_again_from_the_wake() -> None:
    continuity = Continuity(timing())
    continuity.expect(state())
    (wake,) = continuity.due(T0 + timedelta(seconds=900))
    continuity.taken(wake, T0 + timedelta(seconds=900))
    assert continuity.hold("p_abcdef12", DELIVERY, PENDING_DECISION)
    assert continuity.due(T0 + timedelta(days=1)) == []
    # the side's calls while held do not matter: the release sets the start
    continuity.observe("p_abcdef12", "contractor", T0 + timedelta(hours=2))
    released = T0 + timedelta(hours=3)
    assert continuity.release("p_abcdef12", DELIVERY, released)
    assert continuity.due(released + timedelta(seconds=899)) == []
    (step,) = continuity.due(released + timedelta(seconds=900))
    assert step.kind == WAKE  # not the restart that came next before the hold


def test_a_hold_and_a_release_say_whether_they_changed_the_task() -> None:
    """What the hub logs: each change of why a task waits, and its release, nothing more."""
    continuity = Continuity(timing())
    continuity.expect(state(), held=NO_ACCEPTANCE_TEST)
    assert not continuity.hold("p_abcdef12", DELIVERY, NO_ACCEPTANCE_TEST)
    assert continuity.hold("p_abcdef12", DELIVERY, PENDING_DECISION)
    assert continuity.release("p_abcdef12", DELIVERY, T0)
    assert not continuity.release("p_abcdef12", DELIVERY, T0)


def test_a_release_leaves_a_task_that_was_not_held_on_its_step() -> None:
    continuity = Continuity(timing())
    continuity.expect(state())
    (wake,) = continuity.due(T0 + timedelta(seconds=900))
    continuity.taken(wake, T0 + timedelta(seconds=900))
    assert not continuity.release("p_abcdef12", DELIVERY, T0 + timedelta(seconds=1000))
    (step,) = continuity.due(T0 + timedelta(seconds=1800))
    assert step.kind == RESTART


def test_a_held_task_is_answered_by_its_event_and_a_side_without_one_is_left_alone() -> None:
    continuity = Continuity(timing())
    continuity.expect(state(), held=NO_ACCEPTANCE_TEST)
    assert not continuity.hold("p_other999", DELIVERY, PENDING_DECISION)
    assert not continuity.release("p_other999", DELIVERY, T0)
    assert not continuity.release("p_abcdef12", DECISION, T0)
    assert [expectation.side for expectation in continuity.waiting()] == ["contractor"]
    assert continuity.due(T0 + timedelta(days=1)) == []
    assert continuity.answered("p_abcdef12", DELIVERY)
    assert list(continuity.waiting()) == []


def test_a_continuity_started_again_on_its_store_takes_each_ladder_back() -> None:
    """T12 (I10): every change of a task is kept. A hub that starts again neither forgets a
    task nor walks its ladder again from the wake; a held task stays held, for its reason."""
    store = InMemoryContinuityStore()
    before = Continuity(timing(), store)
    before.expect(state())
    (wake,) = before.due(T0 + timedelta(seconds=900))
    before.taken(wake, T0 + timedelta(seconds=900))
    before.observe("p_abcdef12", "contractor", T0 + timedelta(seconds=1000))
    before.expect(
        TaskState("p_zzzzzz99", "contractor", DELIVERY, "base", T0), held=PENDING_DECISION
    )
    before.expect(state("owner"))
    assert before.answered("p_abcdef12", DECISION)

    after = Continuity(timing(), store)
    assert sorted((s.project_id, s.kind) for s in after.waiting()) == [
        ("p_abcdef12", DELIVERY),
        ("p_zzzzzz99", DELIVERY),
    ]
    # the next step is the restart, counted from the last call of the side
    assert after.due(T0 + timedelta(seconds=1899)) == []
    (step,) = after.due(T0 + timedelta(seconds=1900))
    assert (step.kind, step.expectation.project_id) == (RESTART, "p_abcdef12")
    # the held task is still held, and its reason is still known
    assert not after.hold("p_zzzzzz99", DELIVERY, PENDING_DECISION)
    assert after.release("p_zzzzzz99", DELIVERY, T0 + timedelta(days=1))


def test_a_hold_and_a_release_are_kept_for_the_next_start() -> None:
    store = InMemoryContinuityStore()
    Continuity(timing(), store).expect(state())
    assert Continuity(timing(), store).hold("p_abcdef12", DELIVERY, PENDING_DECISION)
    held = Continuity(timing(), store)
    assert held.due(T0 + timedelta(days=1)) == []
    assert not held.hold("p_abcdef12", DELIVERY, PENDING_DECISION)  # the same reason, kept
    released = T0 + timedelta(hours=2)
    assert held.release("p_abcdef12", DELIVERY, released)
    again = Continuity(timing(), store)
    assert again.due(released + timedelta(seconds=899)) == []
    (step,) = again.due(released + timedelta(seconds=900))
    assert step.kind == WAKE


def test_a_step_found_due_for_a_task_since_replaced_is_not_taken() -> None:
    """The watchdog asks what is due, then carries it out; meanwhile the base may be prepared
    again. The new task starts from the wake, whatever the step taken for the old one."""
    continuity = Continuity(timing())
    continuity.expect(state())
    (wake,) = continuity.due(T0 + timedelta(seconds=900))
    renewed = TaskState("p_abcdef12", "contractor", DELIVERY, "the base is ready again", T0)
    continuity.expect(renewed)
    continuity.taken(wake, T0 + timedelta(seconds=900))
    (step,) = continuity.due(T0 + timedelta(seconds=900))
    assert (step.kind, step.expectation) == (WAKE, renewed)


def test_the_delivery_waits_for_a_test_the_gate_runs_and_for_every_decision() -> None:
    service, _, _ = make_service()

    def waits_for() -> str | None:
        return delivery_waits_for(
            service.read(OPERATOR, PROJECT), service.pending(OPERATOR, PROJECT)
        )

    assert waits_for() == NO_ACCEPTANCE_TEST  # no contract yet
    drafted = contract((1, "P1", ("test",), ()))
    drafted["US-E1-01.1"] = {
        "type": "criterion",
        "id": "US-E1-01.1",
        "story": "US-E1-01",
        "text": "c",
    }
    service.apply_reserved(OPERATOR, PROJECT, change(*(put(item) for item in drafted.values())))
    assert waits_for() == NO_ACCEPTANCE_TEST  # a story and its criterion, but no test named yet
    # a criterion names a test, but its story covers nothing: the gate never runs that test
    loose = {"type": "criterion", "id": "US-E1-09.1", "story": "US-E1-09", "text": "c"}
    proposed = service.propose(
        OWNER, PROJECT, change(put(story(1, 9)), put({**loose, "test": "checks/b.py::test_b"}))
    )
    assert waits_for() == PENDING_DECISION  # a decision comes first: it may name the tests
    service.approve(OPERATOR, PROJECT, proposed.id)
    assert waits_for() == NO_ACCEPTANCE_TEST
    named = {**drafted["US-E1-01.1"], "test": "checks/a.py::test_a"}
    proposed = service.propose(OWNER, PROJECT, change(put(named)))
    assert waits_for() == PENDING_DECISION  # the test is proposed, not decided
    service.approve(OPERATOR, PROJECT, proposed.id)
    assert waits_for() is None
    # two changes of this version await a decision: the contract may change again
    service.propose(OWNER, PROJECT, change(put(note(1, "EXG-01"))))
    newer = service.propose(OWNER, PROJECT, change(put(note(2, "EXG-01"))))
    assert waits_for() == PENDING_DECISION
    service.approve(OPERATOR, PROJECT, newer.id)
    # the other one was proposed on an older version: it can no longer be approved
    assert len(service.pending(OPERATOR, PROJECT)) == 1
    assert waits_for() is None


# ----------------------------------------------------------------------------- ADR 0034
def obstacle_task(detail: str = "obstacle OB-001 awaits", side: str = "owner") -> TaskState:
    kind = OBSTACLE if side == "owner" else RAISED
    return TaskState("p_abcdef12", side, kind, detail, T0)


def test_the_owner_side_is_woken_at_once_for_a_blocking_obstacle() -> None:
    continuity = Continuity(timing(restarts=1))
    continuity.await_answer("p_abcdef12", OBSTACLE, obstacle_task())
    (step,) = continuity.due(T0)
    assert (step.kind, step.expectation.kind) == (WAKE, OBSTACLE)
    assert "an obstacle awaits your answer" in step.prompt
    assert "obstacle.read" in step.prompt
    assert "escalate" in step.prompt
    continuity.taken(step, T0)
    (restart,) = continuity.due(T0 + timedelta(seconds=900))
    assert restart.kind == RESTART
    assert "an obstacle still awaits your answer" in restart.prompt


def test_the_main_agent_is_reminded_of_an_obstacle_after_the_inactivity() -> None:
    """The event `obstacle.raised` told it already: its ladder starts from the escalation."""
    continuity = Continuity(timing())
    continuity.await_answer("p_abcdef12", RAISED, obstacle_task(side=MAIN_AGENT))
    assert continuity.due(T0 + timedelta(seconds=899)) == []
    (step,) = continuity.due(T0 + timedelta(seconds=900))
    assert step.kind == ALERT_MAIN


def test_only_an_answer_counts_on_an_obstacle() -> None:
    continuity = Continuity(timing())
    continuity.await_answer("p_abcdef12", OBSTACLE, obstacle_task())
    continuity.observe("p_abcdef12", "owner", T0 + timedelta(seconds=10))
    (step,) = continuity.due(T0 + timedelta(seconds=10))
    assert step.kind == WAKE
    # the same obstacle keeps its place; another one starts again, due at once
    continuity.taken(step, T0 + timedelta(seconds=10))
    continuity.await_answer("p_abcdef12", OBSTACLE, obstacle_task())
    assert continuity.due(T0 + timedelta(seconds=20)) == []
    later = TaskState("p_abcdef12", "owner", OBSTACLE, "obstacle OB-002 awaits", T0)
    continuity.await_answer("p_abcdef12", OBSTACLE, later)
    (again,) = continuity.due(T0 + timedelta(seconds=20))
    assert (again.kind, again.expectation.detail) == (WAKE, "obstacle OB-002 awaits")
    continuity.await_answer("p_abcdef12", OBSTACLE, None)
    assert list(continuity.waiting()) == []


def test_obstacle_tasks_are_apart_from_the_delivery_and_the_decision() -> None:
    assert {OBSTACLE, RAISED} == set(ANSWER_TASKS)
    assert not {DELIVERY, DECISION} & ANSWER_TASKS
