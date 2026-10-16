"""Obstacles (ADR 0034): the evidence, the life of an obstacle, who reads and answers it, the
bounds of a side, and the answers that would show the Contractor an acceptance test."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from zephyros.adapters.memory.clock import FakeClock
from zephyros.adapters.memory.obstacle_store import InMemoryObstacleStore
from zephyros.core.audit import Reason
from zephyros.core.errors import Refusal
from zephyros.core.obstacles import (
    COPIED_CHARS,
    COPY_WINDOW,
    MAX_AWAITING,
    MAX_PER_TURN,
    SHORT_FILE_CHARS,
    Decision,
    Entry,
    Level,
    Obstacle,
    ObstacleService,
    Status,
    check_answer,
    check_close,
    copied_passage,
    deserialise,
    may_read,
    obstacle_summary,
    obstacle_view,
    parse_evidence,
    parse_obstacle_id,
    serialise,
    task_detail,
)
from zephyros.core.ports.agent_engine import Side
from zephyros.core.principals import MAIN_AGENT, OPERATOR, instance_principal

PROJECT = "p_abcdef12"
OTHER = "p_zzzzzz99"
OWNER = instance_principal(Side.OWNER, PROJECT)
CONTRACTOR = instance_principal(Side.CONTRACTOR, PROJECT)
OTHER_OWNER = instance_principal(Side.OWNER, OTHER)
OTHER_CONTRACTOR = instance_principal(Side.CONTRACTOR, OTHER)
T0 = datetime(2026, 10, 16, 8, 0, tzinfo=UTC)

FIELDS: dict[str, object] = {
    "nature": "tool",
    "blocking": True,
    "title": "the simulator says it saves the run and never does",
    "description": "sim save prints 'saving...' and returns 0; no file is ever written.",
    "tried": "ran it three times, with and without --sync; waited ten minutes",
    "needed": "a simulator that writes the run, or another way to read the result",
}
EVIDENCE = (
    {"kind": "command", "command": "sim save run.dat", "output": "saving...", "exit_code": 0},
)


def service() -> tuple[ObstacleService, FakeClock]:
    clock = FakeClock(T0)
    return ObstacleService(InMemoryObstacleStore(), clock), clock


def report(
    obstacles: ObstacleService,
    who: Any = CONTRACTOR,
    *,
    turn: datetime | None = T0,
    **changes: object,
) -> Obstacle:
    return obstacles.report(who, who.project_id, {**FIELDS, **changes}, EVIDENCE, turn_started=turn)


def entry(decision: Decision, by: str = OWNER.id, **fields: Any) -> Entry:
    return Entry(decision, by, T0, Level.OWNER, fields.pop("text", "an answer"), **fields)


# ----------------------------------------------------------------------------- the evidence
def test_each_kind_of_evidence_takes_its_own_fields() -> None:
    parsed = parse_evidence(
        [
            {"kind": "path", "path": "/work/out/run.log", "note": "empty"},
            {"kind": "path", "path": "/opt/project-tools/bin/sim"},
            {"kind": "command", "command": "sim save", "output": "saving...", "exit_code": 0},
            {"kind": "delivery", "delivery_id": "d3"},
            {"kind": "log", "seq": 1234},
        ]
    )
    assert [item["kind"] for item in parsed] == ["path", "path", "command", "delivery", "log"]
    assert parsed[2] == {
        "kind": "command",
        "command": "sim save",
        "exit_code": 0,
        "output": "saving...",
    }


@pytest.mark.parametrize(
    ("item", "said"),
    [
        ({"kind": "path"}, "a path names its path"),
        ({"kind": "command", "output": "x"}, "a command names its command"),
        ({"kind": "delivery", "delivery_id": "d1", "seq": 3}, "seq is not a field of a delivery"),
        ({"kind": "log", "seq": 3, "path": "/work"}, "path is not a field of a log"),
        ({"kind": "path", "path": "/etc/passwd"}, "under /work"),
        ({"kind": "path", "path": "/work/../etc/passwd"}, "without '..'"),
        ({"kind": "path", "path": "/workshop/x"}, "under /work"),
        ({"kind": "path", "path": "/opt/project-tools-2/x"}, "under /work"),
    ],
)
def test_evidence_out_of_its_kind_or_out_of_the_instance_is_refused(
    item: dict[str, object], said: str
) -> None:
    with pytest.raises(Refusal) as refused:
        parse_evidence([item])
    assert refused.value.reason is Reason.INVALID_REQUEST
    assert said in str(refused.value)


# ----------------------------------------------------------------------------- copies
TEST_FILE = (
    "import subprocess\n\n\ndef test_score():\n"
    "    out = subprocess.run(['sim', 'score', 'run.dat'], capture_output=True, text=True)\n"
    "    assert out.returncode == 0, out.stderr\n"
    "    assert float(out.stdout) >= 0.87, 'the run scores at least 0.87 on the hidden track'\n"
)


def test_an_answer_that_copies_a_long_passage_of_a_test_is_found() -> None:
    passage = TEST_FILE[40:240]
    assert len(" ".join(passage.split())) >= COPIED_CHARS
    reindented = "Here is what it checks:\n" + "  ".join(passage.split()) + "\nThat is all."
    assert copied_passage(reindented, {"tests/test_score.py": TEST_FILE.encode()}) == (
        "tests/test_score.py"
    )


def test_an_answer_in_its_own_words_or_a_short_quote_is_not() -> None:
    files = {"tests/test_score.py": TEST_FILE.encode(), "tests/empty.py": b""}
    own_words = "The test runs the simulator's score command on your run and wants 0.87 or more."
    assert copied_passage(own_words, files) is None
    assert copied_passage(TEST_FILE[100 : 100 + COPY_WINDOW - 1], files) is None
    assert copied_passage("", files) is None
    assert copied_passage(TEST_FILE, {}) is None


@pytest.mark.parametrize(
    "mark",
    [
        lambda n, line: f"> {line}",  # a quote
        lambda n, line: f">> {line}",
        lambda n, line: f"{n}: {line}",  # a numbered listing
        lambda n, line: f"{n:>6}\t{line}",  # as `cat -n` shows it
        lambda n, line: f"{n}| {line}",
        lambda n, line: f"+{line}",  # a diff
        lambda n, line: f"-{line}",
    ],
)
def test_a_copy_with_a_mark_before_each_line_is_found(mark: Any) -> None:
    quoted = "\n".join(mark(n, line) for n, line in enumerate(TEST_FILE.splitlines(), 1))
    assert copied_passage(f"The test:\n{quoted}", {"tests/test_score.py": TEST_FILE.encode()})


def test_a_short_test_copied_whole_is_found() -> None:
    short = "def test_answer():\n    assert answer() == 42\n"
    assert SHORT_FILE_CHARS <= len(" ".join(short.split())) < COPY_WINDOW
    files = {"t.py": short.encode(), "tiny.py": b"import os\n"}
    assert copied_passage("Look:\n> def test_answer():\n>     assert answer() == 42", files)
    assert copied_passage("it checks that answer() == 42", files) is None  # a part of it
    assert copied_passage("We use: import os", files) is None  # under SHORT_FILE_CHARS


def test_every_copied_passage_of_the_bound_is_found_wherever_it_starts() -> None:
    """A passage of COPIED_CHARS characters holds a whole window of the answer, whatever its
    offset in the answer: the stride never lets it through."""
    words = " ".join(f"w{n:03d}" for n in range(400))
    files = {"t.py": words.encode()}
    for offset in range(60):
        answer = "x" * offset + " " + words[500 : 500 + COPIED_CHARS] + " y"
        assert copied_passage(answer, files) == "t.py", offset


# ----------------------------------------------------------------------------- who reads
def test_each_reads_the_obstacles_that_concern_it() -> None:
    obstacles, _clock = service()
    of_contractor = report(obstacles)
    of_owner = report(obstacles, OWNER)
    assert may_read(OPERATOR, of_owner)
    assert may_read(MAIN_AGENT, of_contractor)
    assert may_read(OWNER, of_contractor)
    assert may_read(OWNER, of_owner)
    assert may_read(CONTRACTOR, of_contractor)
    # never what the Owner side reported: it may speak of the tests it keeps hidden
    assert not may_read(CONTRACTOR, of_owner)
    # nor anything of another project
    assert not may_read(OTHER_OWNER, of_contractor)
    assert not may_read(OTHER_CONTRACTOR, of_contractor)


def test_a_reader_it_does_not_concern_is_refused_and_an_unknown_one_is_not_found() -> None:
    obstacles, _clock = service()
    of_owner = report(obstacles, OWNER)
    with pytest.raises(Refusal) as refused:
        obstacles.get(CONTRACTOR, PROJECT, of_owner.id)
    assert refused.value.reason is Reason.FORBIDDEN
    with pytest.raises(Refusal) as missing:
        obstacles.get(OWNER, PROJECT, "OB-042")
    assert missing.value.reason is Reason.NOT_FOUND
    with pytest.raises(Refusal) as malformed:
        parse_obstacle_id("OB-1")
    assert malformed.value.reason is Reason.INVALID_REQUEST


# ----------------------------------------------------------------------------- the life
def test_an_obstacle_of_the_contractor_awaits_the_owner_side_then_the_operator() -> None:
    obstacles, _clock = service()
    first = report(obstacles)
    assert (first.id, first.status, first.level, first.awaiting) == (
        "OB-001",
        Status.OPEN,
        Level.OWNER,
        True,
    )
    _before, escalated = obstacles.answer(OWNER, PROJECT, "OB-001", entry(Decision.ESCALATE))
    assert (escalated.status, escalated.level) == (Status.ESCALATED, Level.OPERATOR)
    assert escalated.entries[-1].level is Level.OWNER  # where it stood when it was taken
    _before, human = obstacles.answer(
        MAIN_AGENT, PROJECT, "OB-001", entry(Decision.ESCALATE, MAIN_AGENT.id)
    )
    assert (human.status, human.level) == (Status.ESCALATED, Level.HUMAN)
    _before, handled = obstacles.answer(
        OPERATOR, PROJECT, "OB-001", entry(Decision.HANDLE, OPERATOR.id, action="new_session")
    )
    assert (handled.status, handled.level, handled.awaiting) == (
        Status.ANSWERED,
        Level.HUMAN,
        False,
    )
    closed = obstacles.close(CONTRACTOR, PROJECT, "OB-001", "it saves now")
    assert closed.status is Status.CLOSED
    assert [e.decision for e in closed.entries] == [
        Decision.ESCALATE,
        Decision.ESCALATE,
        Decision.HANDLE,
        Decision.CLOSE,
    ]


def test_an_obstacle_of_the_owner_side_awaits_the_operator() -> None:
    obstacles, _clock = service()
    mine = report(obstacles, OWNER)
    assert (mine.level, mine.side) == (Level.OPERATOR, Side.OWNER)
    with pytest.raises(Refusal) as refused:  # its author never answers it
        check_answer(OWNER, mine, Decision.CLARIFY)
    assert refused.value.reason is Reason.FORBIDDEN


@pytest.mark.parametrize(
    ("level", "who", "allowed"),
    [
        (Level.OWNER, OWNER, True),
        (Level.OWNER, OPERATOR, True),
        (Level.OWNER, MAIN_AGENT, False),
        (Level.OWNER, OTHER_OWNER, False),
        (Level.OWNER, CONTRACTOR, False),
        (Level.OPERATOR, OPERATOR, True),
        (Level.OPERATOR, MAIN_AGENT, True),
        (Level.OPERATOR, OWNER, False),
        (Level.HUMAN, OPERATOR, True),
        (Level.HUMAN, MAIN_AGENT, False),
        (Level.HUMAN, OWNER, False),
    ],
)
def test_who_answers_at_each_level(level: Level, who: Any, allowed: bool) -> None:
    obstacles, _clock = service()
    standing = Obstacle(**{**vars_of(report(obstacles)), "level": level})
    if allowed:
        check_answer(who, standing, Decision.REJECT)
        return
    with pytest.raises(Refusal) as refused:
        check_answer(who, standing, Decision.REJECT)
    assert refused.value.reason is Reason.FORBIDDEN


def vars_of(obstacle: Obstacle) -> dict[str, Any]:
    return {name: getattr(obstacle, name) for name in Obstacle.__slots__}


def test_each_level_takes_its_own_decisions() -> None:
    obstacles, _clock = service()
    at_owner = report(obstacles)
    with pytest.raises(Refusal) as handle:  # handling is the operator's
        check_answer(OWNER, at_owner, Decision.HANDLE)
    assert handle.value.reason is Reason.INVALID_REQUEST
    at_human = Obstacle(**{**vars_of(at_owner), "level": Level.HUMAN})
    with pytest.raises(Refusal) as above:  # nothing above the human
        check_answer(OPERATOR, at_human, Decision.ESCALATE)
    assert above.value.reason is Reason.INVALID_REQUEST


def test_an_obstacle_answered_awaits_nothing_more() -> None:
    obstacles, _clock = service()
    report(obstacles)
    obstacles.answer(OWNER, PROJECT, "OB-001", entry(Decision.REJECT, text="a fault of d3"))
    with pytest.raises(Refusal) as again:
        obstacles.answer(OWNER, PROJECT, "OB-001", entry(Decision.CLARIFY))
    assert again.value.reason is Reason.NOT_PENDING


def test_its_author_or_the_operator_closes_an_obstacle() -> None:
    obstacles, _clock = service()
    standing = report(obstacles)
    check_close(CONTRACTOR, standing)  # withdrawn before any answer
    check_close(OPERATOR, standing)
    for other in (OWNER, MAIN_AGENT):
        with pytest.raises(Refusal) as refused:
            check_close(other, standing)
        assert refused.value.reason is Reason.FORBIDDEN
    closed = obstacles.close(OPERATOR, PROJECT, standing.id, "")
    with pytest.raises(Refusal) as twice:
        check_close(OPERATOR, closed)
    assert twice.value.reason is Reason.NOT_PENDING


def test_a_check_of_the_caller_refuses_before_anything_is_written() -> None:
    obstacles, _clock = service()
    report(obstacles)

    def refuse(_obstacle: Obstacle) -> None:
        raise Refusal(Reason.ACCEPTANCE_LEAK, "no")

    with pytest.raises(Refusal):
        obstacles.answer(OWNER, PROJECT, "OB-001", entry(Decision.CLARIFY), refuse)
    with pytest.raises(Refusal):
        obstacles.close(OPERATOR, PROJECT, "OB-001", "note", refuse)
    assert obstacles.get(OPERATOR, PROJECT, "OB-001").status is Status.OPEN


# ----------------------------------------------------------------------------- the bounds
def test_a_side_reports_a_bounded_number_in_a_turn() -> None:
    obstacles, clock = service()
    for _ in range(MAX_PER_TURN):
        report(obstacles, blocking=False)
    with pytest.raises(Refusal) as refused:
        report(obstacles)
    assert refused.value.reason is Reason.TOO_MANY_OBSTACLES
    # the other side has its own bound; a new turn starts the count again
    report(obstacles, OWNER)
    clock.advance(60)
    assert report(obstacles, turn=clock.now()).id == "OB-005"


def test_withdrawing_an_obstacle_does_not_give_a_turn_more() -> None:
    obstacles, _clock = service()
    for _ in range(MAX_PER_TURN):
        withdrawn = report(obstacles)
        obstacles.close(CONTRACTOR, PROJECT, withdrawn.id, "")
    with pytest.raises(Refusal) as refused:
        report(obstacles)
    assert refused.value.reason is Reason.TOO_MANY_OBSTACLES


def test_where_the_turn_is_unknown_an_hour_counts() -> None:
    obstacles, clock = service()
    for _ in range(MAX_PER_TURN):
        report(obstacles, turn=None)
    with pytest.raises(Refusal):
        report(obstacles, turn=None)
    clock.advance(3601)
    report(obstacles, turn=None)


def test_a_side_has_a_bounded_number_awaiting_an_answer() -> None:
    obstacles, clock = service()
    for number in range(MAX_AWAITING):
        clock.advance(60)
        report(obstacles, turn=clock.now(), title=f"obstacle {number}")
    clock.advance(60)
    with pytest.raises(Refusal) as refused:
        report(obstacles, turn=clock.now())
    assert refused.value.reason is Reason.TOO_MANY_OBSTACLES
    assert "at most 5" in str(refused.value)
    # an answer frees a place
    obstacles.answer(OWNER, PROJECT, "OB-001", entry(Decision.CLARIFY))
    assert report(obstacles, turn=clock.now()).id == "OB-006"


def test_an_instance_reports_for_its_own_project_only() -> None:
    obstacles, _clock = service()
    with pytest.raises(Refusal) as refused:
        obstacles.report(CONTRACTOR, OTHER, FIELDS, EVIDENCE, turn_started=T0)
    assert refused.value.reason is Reason.FORBIDDEN
    with pytest.raises(Refusal):
        obstacles.report(OPERATOR, PROJECT, FIELDS, EVIDENCE, turn_started=T0)


# ----------------------------------------------------------------------------- the author
def test_its_author_sees_the_answer_once_read_or_briefed() -> None:
    obstacles, _clock = service()
    report(obstacles)
    assert obstacles.brief(PROJECT, Side.CONTRACTOR) is None
    briefing = obstacles.brief(PROJECT, Side.OWNER)
    assert briefing is not None
    assert briefing.awaiting == ("OB-001",)
    assert "OB-001 (blocking)" in briefing.text
    obstacles.answer(OWNER, PROJECT, "OB-001", entry(Decision.CLARIFY))
    told = obstacles.brief(PROJECT, Side.CONTRACTOR)
    assert told is not None
    assert told.answered == ("OB-001",)
    assert "obstacle.read" in told.text
    assert obstacles.brief(PROJECT, Side.OWNER) is None  # nothing awaits the Owner side now
    obstacles.briefed(PROJECT, told)
    assert obstacles.brief(PROJECT, Side.CONTRACTOR) is None
    # a later decision is news again, until the author reads it
    report(obstacles, blocking=False)
    obstacles.answer(OWNER, PROJECT, "OB-002", entry(Decision.REJECT))
    assert obstacles.brief(PROJECT, Side.CONTRACTOR) is not None
    obstacles.read(CONTRACTOR, PROJECT, "OB-002")
    assert obstacles.brief(PROJECT, Side.CONTRACTOR) is None


def test_a_decision_taken_after_the_briefing_is_still_news() -> None:
    """A decision that comes while the turn starts was not in its prompt: the author is told of
    it at the next turn."""
    obstacles, _clock = service()
    report(obstacles)
    obstacles.answer(OWNER, PROJECT, "OB-001", entry(Decision.ESCALATE))
    told = obstacles.brief(PROJECT, Side.CONTRACTOR)
    assert told is not None
    assert told.told == (1,)
    obstacles.answer(OPERATOR, PROJECT, "OB-001", entry(Decision.CLARIFY, OPERATOR.id))
    obstacles.briefed(PROJECT, told)
    again = obstacles.brief(PROJECT, Side.CONTRACTOR)
    assert again is not None
    assert (again.answered, again.told) == (("OB-001",), (2,))


def test_a_reader_other_than_its_author_changes_nothing() -> None:
    obstacles, _clock = service()
    report(obstacles)
    obstacles.answer(OWNER, PROJECT, "OB-001", entry(Decision.CLARIFY))
    obstacles.read(OWNER, PROJECT, "OB-001")
    obstacles.read(OPERATOR, PROJECT, "OB-001")
    assert obstacles.get(OPERATOR, PROJECT, "OB-001").unseen


def test_a_briefing_names_a_bounded_number() -> None:
    obstacles, clock = service()
    for _ in range(4):
        for _ in range(MAX_PER_TURN):
            report(obstacles, turn=clock.now(), blocking=False)
            obstacles.answer(
                OWNER, PROJECT, obstacles.of_project(PROJECT)[-1].id, entry(Decision.REJECT)
            )
        clock.advance(3601)
    briefing = obstacles.brief(PROJECT, Side.CONTRACTOR)
    assert briefing is not None
    assert len(briefing.answered) == 12
    assert "and 7 more" in briefing.text


# ----------------------------------------------------------------------------- the task
def test_the_oldest_blocking_obstacle_awaiting_a_level_is_the_task() -> None:
    obstacles, clock = service()
    report(obstacles, blocking=False)
    clock.advance(1)
    report(obstacles)
    clock.advance(1)
    report(obstacles)
    found = obstacles.oldest_blocking(PROJECT, Level.OWNER)
    assert found is not None
    assert found.id == "OB-002"
    assert obstacles.oldest_blocking(PROJECT, Level.OPERATOR) is None
    assert task_detail(found) == (
        "obstacle OB-002 (tool, blocking) of the Contractor awaits an answer of the Owner side"
    )


# ----------------------------------------------------------------------------- the record
def test_an_obstacle_is_kept_as_it_is() -> None:
    obstacles, _clock = service()
    report(obstacles)
    obstacles.answer(
        OWNER,
        PROJECT,
        "OB-001",
        entry(Decision.FIX_TEST, paths=("tests/test_score.py",), text="fixed"),
    )
    kept = obstacles.get(OPERATOR, PROJECT, "OB-001")
    assert deserialise(serialise(kept)) == kept


def test_a_reader_gets_the_whole_obstacle_and_a_list_one_line_of_it() -> None:
    obstacles, clock = service()
    report(obstacles)
    clock.advance(30)
    _before, answered = obstacles.answer(
        OWNER, PROJECT, "OB-001", entry(Decision.AMEND, change_id="c_0001", text="see c_0001")
    )
    view = obstacle_view(answered)
    assert view["obstacle_id"] == "OB-001"
    assert view["awaits"] is None
    assert view["evidence"] == [dict(item) for item in EVIDENCE]
    assert view["decisions"] == [
        {
            "decision": "amend",
            "by": OWNER.id,
            "at": "2026-10-16T08:00:00.000000Z",
            "level": "owner",
            "text": "see c_0001",
            "change_id": "c_0001",
        }
    ]
    assert "seen" not in view
    line = obstacle_summary(answered)
    assert (line["status"], line["last_decision"], line["awaits"]) == ("answered", "amend", None)
    assert line["updated_at"] == "2026-10-16T08:00:00.000000Z"


def test_obstacles_are_numbered_per_project() -> None:
    obstacles, _clock = service()
    assert report(obstacles).id == "OB-001"
    assert report(obstacles, OTHER_CONTRACTOR).id == "OB-001"
    assert report(obstacles, OWNER).id == "OB-002"
    assert [o.id for o in obstacles.visible(OPERATOR, [PROJECT, OTHER])] == [
        "OB-002",
        "OB-001",
        "OB-001",
    ]
    assert [o.id for o in obstacles.visible(CONTRACTOR, [PROJECT])] == ["OB-001"]


def test_time_moves_the_list_not_the_numbers() -> None:
    obstacles, clock = service()
    report(obstacles)
    clock.advance(int(timedelta(minutes=5).total_seconds()))
    report(obstacles)
    assert [o.id for o in obstacles.visible(OWNER, [PROJECT])] == ["OB-002", "OB-001"]
