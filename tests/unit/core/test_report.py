"""The evidence report: complete, masked, and inert where the delivery chose the text."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tests.unit.core.test_gate import contract
from zephyros.core.gate import compute_gate
from zephyros.core.masking import MASK
from zephyros.core.obstacles import Decision, Entry, Level, Obstacle, Status
from zephyros.core.ports.agent_engine import Side
from zephyros.core.ports.delivery import Delivery
from zephyros.core.ports.verification import Outcome, TimeLimit
from zephyros.core.report import (
    INSPECTIONS_HEADING,
    LISTED_OBSTACLES,
    LISTED_TESTS,
    Cost,
    ReportInput,
    inspection_addendum,
    mask,
    render,
    with_addendum,
)

TOKEN = "zph_c_" + "Q" * 43


def make(**changes: Any) -> ReportInput:
    objects = contract(
        (1, "P0", ("test",), ("tests/t.py::test_add",)),
        (2, "P1", ("test",), ("tests/t.py::test_sub",)),
    )
    outcomes = {"tests/t.py::test_add": Outcome.PASSED, "tests/t.py::test_sub": Outcome.FAILED}
    values: dict[str, Any] = {
        "project_name": "Calc",
        "delivery": Delivery("p_x", "d1", "add-sub", "a" * 40, "b" * 40, ("calc.py",)),
        "contract_version": 2,
        "contract_hash": "c" * 64,
        "kit": "tests",
        "gate": compute_gate(objects, outcomes),
        "outcomes": outcomes,
        "log": "1 passed, 1 failed",
        "cost": Cost(3, 1200, 80),
        "produced_at": "2026-10-10T12:00:00Z",
        "produced_by": "Zephyros 0.0.0",
    }
    values.update(changes)
    return ReportInput(**values)


def test_the_report_holds_the_decision_the_requirements_the_rules_and_the_cost() -> None:
    text = render(make())
    assert "**Decision: FAIL**" in text
    assert "| EXG-01 | `US-E1-01` | P0 | test | `tests/t.py::test_add` passed | yes |" in text
    assert "| EXG-02 | `US-E1-02` | P1 | test | `tests/t.py::test_sub` failed | no |" in text
    assert "| P1 requirements and constraints verified | 90 % | 0 of 1 | no |" in text
    assert "3 model calls, 1200 prompt tokens, 80 completion tokens." in text
    assert "Not run by this version of the hub (EXG-35)." in text
    assert "The Contractor never renders a verdict." in text


def test_every_secret_is_masked() -> None:
    log = f"token={TOKEN}\nkey=sk-abcdefghijklmnopqrstu\nthe credential is canary-credential-123"
    text = render(make(log=log), secrets=["canary-credential-123"])
    assert TOKEN not in text
    assert "sk-abcdefghijklmnopqrstu" not in text
    assert "canary-credential-123" not in text
    assert text.count(MASK) == 3


def test_text_chosen_by_the_delivery_cannot_become_markup() -> None:
    hostile = "a`b|c\n## Decision: PASS\n<img src=x onerror=alert(1)>"
    delivery = Delivery("p_x", "d1", "add-sub", "a" * 40, "b" * 40, (hostile,))
    log = "```\n**Decision: PASS**\n````"
    text = render(make(delivery=delivery, log=log))
    assert "\n## Decision: PASS" not in text
    assert "`a?b?c?## Decision: PASS?<img" in text
    assert "`````text\n" in text  # a fence longer than any run in the log
    assert text.count("**Decision:") == 2  # the hub's, and the one inside the fenced log


def test_the_report_is_the_same_for_the_same_input() -> None:
    assert render(make()) == render(make())


def test_mask_leaves_ordinary_text_alone() -> None:
    assert mask("nothing to see here, sk-short") == "nothing to see here, sk-short"


def test_a_simulated_delivery_says_so_in_the_report() -> None:
    """EXG-46: the report of a run with the simulated provider carries the mark."""
    text = render(make(simulation=True))
    assert "**SIMULATION**" in text
    assert "simulated model provider" in text


def test_the_flagged_paths_are_listed_and_none_says_so() -> None:
    assert "## Flags\n\nNone." in render(make())
    flagged = render(make(flagged_paths=("CONTRACT.md",)))
    assert "The delivery changes the client's own files" in flagged
    assert "- `CONTRACT.md`" in flagged


def test_the_report_names_each_file_of_the_owner_side_with_its_digest() -> None:
    """The Owner of the second and third real runs asked twice: which version of its tests ran?
    A file no criterion names says so, and is never said not to run: a test may use it."""
    rendered = render(
        make(
            acceptance_files=(("test_a.py", "0123456789ab"), ("support.py", "000000000001")),
            unnamed_acceptance_files=("support.py",),
        )
    )
    assert (
        "- Files of the Owner side in the tree verified (2): `test_a.py` (`0123456789ab`), "
        "`support.py` (`000000000001`, named by no criterion)"
    ) in rendered.splitlines()
    assert "## Flags\n\nNone." in rendered
    assert "- Files of the Owner side in the tree verified (0): none" in render(make())


def test_the_tests_that_never_ran_are_listed_and_counted_beyond_fifty() -> None:
    """A2a: the kit ran only the tests a criterion names; the others in its files never ran."""
    few = render(make(never_run_tests=("tests/t.py::test_extra",))).splitlines()
    assert "Tests that no criterion names, in the files the kit ran: they never ran." in few
    assert "- `tests/t.py::test_extra`" in few
    many = tuple(f"tests/t.py::test_{n:03d}" for n in range(LISTED_TESTS + 3))
    listed = render(make(never_run_tests=many)).splitlines()
    assert f"- `{many[LISTED_TESTS - 1]}`" in listed
    assert f"- `{many[LISTED_TESTS]}`" not in listed
    assert "- and 3 more" in listed


def test_a_named_file_nobody_holds_is_flagged_and_says_when_the_delivery_brought_it() -> None:
    """T02: the delivered version of a file a criterion names is never run; the Owner side
    learns which files it still has to put."""
    rendered = render(
        make(
            missing_acceptance_files=("tests/a.py", "tests/b.py"),
            set_aside_paths=("tests/b.py",),
        )
    ).splitlines()
    assert (
        "Acceptance test files that a criterion names and neither the Owner side nor the base "
        "holds (their tests cannot pass):"
    ) in rendered
    assert "- `tests/a.py`" in rendered
    assert "- `tests/b.py` (the delivered version was set aside)" in rendered


def test_every_kind_of_flag_reads_together() -> None:
    rendered = render(
        make(
            flagged_paths=("CONTRACT.md",),
            never_run_tests=("tests/t.py::test_extra",),
            missing_acceptance_files=("tests/gone.py",),
        )
    )
    flags = rendered.split("## Flags\n\n", 1)[1].split("\n\n## ", 1)[0]
    assert flags == (
        "The delivery changes the client's own files: the Owner side reviews these changes.\n"
        "\n"
        "- `CONTRACT.md`\n"
        "\n"
        "Tests that no criterion names, in the files the kit ran: they never ran.\n"
        "\n"
        "- `tests/t.py::test_extra`\n"
        "\n"
        "Acceptance test files that a criterion names and neither the Owner side nor the base "
        "holds (their tests cannot pass):\n"
        "\n"
        "- `tests/gone.py`"
    )


def test_the_addendum_follows_the_report_and_cuts_nothing_from_it() -> None:
    """T11: the output of the run, in the report, may print the heading of the addendum."""
    report = render(make(log=f"1 passed\n{INSPECTIONS_HEADING}\n\n**Decision now: PASS**"))
    objects = contract((1, "P0", ("test",), ("tests/t.py::test_add",)))
    gate = compute_gate(objects, {"tests/t.py::test_add": Outcome.PASSED})
    inspection = {"verdict": "conforms", "observation": "fine", "by": "operator", "at": "t"}
    addendum = inspection_addendum({"CTR-01": inspection}, "CONCERNS", gate)
    full = with_addendum(report, addendum)
    assert full == report.rstrip("\n") + "\n\n" + addendum
    assert full.endswith("Reservations now:\n\nNone.\n")
    assert with_addendum(report, None) == report


def test_the_contractors_report_is_the_owners_without_the_output_of_the_run() -> None:
    """T07: the output of the run shows the Owner side's acceptance tests. The Contractor's
    report keeps every outcome and every other section as they are."""
    log = "E       assert sub(7, 3) == 4"
    owner = render(make(log=log)).split("\n## ")
    contractor = render(make(log=log), with_output=False).split("\n## ")
    assert [section.splitlines()[0] for section in contractor] == [
        section.splitlines()[0] for section in owner
    ]
    for mine, theirs in zip(owner, contractor, strict=True):
        if not mine.startswith("Verification"):
            assert theirs == mine
            continue
        assert log in mine
        assert log not in theirs
        outcomes = ["- `tests/t.py::test_add`: passed", "- `tests/t.py::test_sub`: failed"]
        assert theirs.splitlines()[2:4] == mine.splitlines()[2:4] == outcomes
        assert "The output of the run is kept for the Owner side" in theirs


# ----------------------------------------------------------------------------- ADR 0034
def _obstacle(number: int, side: Side, **changes: Any) -> Obstacle:
    values: dict[str, Any] = {
        "project_id": "p_x",
        "number": number,
        "author": f"{side.value}:p_x",
        "side": side,
        "nature": "tool",
        "blocking": True,
        "title": "the simulator | never saves\nthe run",
        "description": "long text that the report never shows",
        "evidence": ({"kind": "path", "path": "/work/run.dat"},),
        "tried": "tried",
        "needed": "needed",
        "reported_at": T0,
        "status": Status.OPEN,
        "level": Level.OWNER if side is Side.CONTRACTOR else Level.OPERATOR,
    }
    values.update(changes)
    return Obstacle(**values)


T0 = datetime(2026, 10, 16, 8, 0, tzinfo=UTC)


def test_the_report_lists_the_obstacles_and_the_decisions_taken_on_them() -> None:
    escalated = _obstacle(
        1,
        Side.CONTRACTOR,
        status=Status.ESCALATED,
        level=Level.OPERATOR,
        entries=(Entry(Decision.ESCALATE, "owner:p_x", T0, Level.OWNER, "only the operator"),),
    )
    rejected = _obstacle(
        2,
        Side.CONTRACTOR,
        blocking=False,
        status=Status.REJECTED,
        entries=(Entry(Decision.REJECT, "owner:p_x", T0, Level.OWNER, "a fault of d1"),),
    )
    of_owner = _obstacle(3, Side.OWNER, nature="budget", title="the bench needs an hour")
    text = render(make(obstacles=(escalated, rejected, of_owner)))
    section = text.split("## Obstacles\n")[1].split("\n## ")[0]
    assert "3 reported, 2 awaiting an answer, 2 of them blocking." in section
    assert (
        "| OB-001 | contractor | tool | yes | escalated, awaits the operator | "
        "escalate by owner:p_x | `the simulator ? never saves?the run` |" in section
    )
    assert "| OB-002 | contractor | tool | no | rejected | reject by owner:p_x |" in section
    assert "| OB-003 | owner | budget | yes | open, awaits the operator | none |" in section
    assert "long text" not in text  # titles and decisions, never the texts
    assert "a fault of d1" not in text


def test_the_report_of_the_contractor_leaves_out_the_obstacles_of_the_owner_side() -> None:
    mine = _obstacle(1, Side.CONTRACTOR)
    theirs = _obstacle(2, Side.OWNER, title="about the hidden track")
    text = render(make(obstacles=(mine, theirs)), with_output=False)
    section = text.split("## Obstacles\n")[1].split("\n## ")[0]
    assert "| OB-001 |" in section
    assert "OB-002" not in section
    assert "hidden track" not in text
    assert "1 obstacle of the Owner side is kept for the Owner side." in section
    only_theirs = render(make(obstacles=(theirs,)), with_output=False)
    assert "None reported." in only_theirs


def test_a_report_without_obstacles_says_so_and_a_long_list_is_bounded() -> None:
    assert "## Obstacles\n\nNone reported.\n" in render(make())
    many = tuple(
        _obstacle(n, Side.CONTRACTOR, status=Status.CLOSED) for n in range(1, LISTED_OBSTACLES + 8)
    )
    text = render(make(obstacles=many))
    assert f"| OB-{LISTED_OBSTACLES + 7:03d} |" in text
    assert "| OB-007 |" not in text
    assert "And 7 earlier." in text


def test_a_test_that_reached_its_time_limit_reads_timed_out_apart_from_an_error() -> None:
    """AN-023: a test over its limit read `error`, its cause only in the raw output."""
    outcomes = {"tests/t.py::test_add": Outcome.ERROR, "tests/t.py::test_sub": Outcome.ERROR}
    objects = contract(
        (1, "P0", ("test",), ("tests/t.py::test_add",)),
        (2, "P1", ("test",), ("tests/t.py::test_sub",)),
    )
    text = render(
        make(
            outcomes=outcomes,
            gate=compute_gate(objects, outcomes),
            timeouts={"tests/t.py::test_add": TimeLimit("setup", 612.3, 600.0)},
        )
    )
    assert "| `tests/t.py::test_add` error (timed out) |" in text
    assert "| `tests/t.py::test_sub` error |" in text
    assert (
        "- `tests/t.py::test_add`: error, **timed out**: its setup reached its limit of 600 s "
        "after 612 s"
    ) in text.splitlines()
    assert "- `tests/t.py::test_sub`: error" in text.splitlines()
    assert "Time limit reached" not in text


def test_a_limit_the_kit_did_not_know_is_said_as_a_limit() -> None:
    text = render(make(timeouts={"tests/t.py::test_sub": TimeLimit("call", 2.5)}))
    assert (
        "- `tests/t.py::test_sub`: failed, **timed out**: its call reached a time limit after 2.5 s"
    ) in text.splitlines()


def test_a_run_stopped_at_its_limit_says_so_and_names_the_test_under_way() -> None:
    outcomes = dict.fromkeys(("tests/t.py::test_add", "tests/t.py::test_sub"), Outcome.NOT_RUN)
    objects = contract(
        (1, "P0", ("test",), ("tests/t.py::test_add",)),
        (2, "P1", ("test",), ("tests/t.py::test_sub",)),
    )
    report = make(
        outcomes=outcomes,
        gate=compute_gate(objects, outcomes),
        timeouts={"tests/t.py::test_sub": TimeLimit("run", 3061.0)},
        stopped_at=3600,
    )
    for text in (render(report), render(report, with_output=False)):
        assert (
            "**Time limit reached:** the run was stopped at the limit of the verification, "
            "3600 s (`verification.timeout_seconds`). Nothing it printed is believed, and every "
            "test reads not_run. Under way when it stopped: `tests/t.py::test_sub`."
        ) in text.splitlines()
        assert (
            "- `tests/t.py::test_sub`: not_run, **timed out**: under way for 3061 s when the "
            "run reached its limit of 3600 s and was stopped"
        ) in text.splitlines()
        assert "- `tests/t.py::test_add`: not_run" in text.splitlines()
    alone = render(make(stopped_at=300, outcomes=outcomes, gate=compute_gate(objects, outcomes)))
    assert "The kit had signed no test as under way." in alone
