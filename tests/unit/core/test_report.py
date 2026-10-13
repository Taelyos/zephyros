"""The evidence report: complete, masked, and inert where the delivery chose the text."""

from __future__ import annotations

from typing import Any

from tests.unit.core.test_gate import contract
from zephyros.core.gate import compute_gate
from zephyros.core.masking import MASK
from zephyros.core.ports.delivery import Delivery
from zephyros.core.ports.verification import Outcome
from zephyros.core.report import (
    INSPECTIONS_HEADING,
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


def test_the_report_names_each_acceptance_test_file_it_ran_with_its_digest() -> None:
    """The Owner of the second and third real runs asked twice: which version of its tests ran?"""
    rendered = render(make(acceptance_files=(("test_a.py", "0123456789ab"),)))
    assert "- Acceptance tests of the Owner side (1): `test_a.py` (`0123456789ab`)" in rendered
    assert "- Acceptance tests of the Owner side (0): none" in render(make())


def test_an_acceptance_file_that_no_criterion_names_is_flagged() -> None:
    """C49: the Owner of the third real run noticed a test that never ran; the report says so."""
    rendered = render(make(unnamed_acceptance_files=("tests/extra.py",)))
    assert "Acceptance test files that no criterion names (they never run):" in rendered
    assert "- `tests/extra.py`" in rendered
    assert "## Flags\n\nNone." in render(make())  # nothing unnamed, nothing flagged


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
            unnamed_acceptance_files=("tests/extra.py",),
            missing_acceptance_files=("tests/gone.py",),
        )
    )
    flags = rendered.split("## Flags\n\n", 1)[1].split("\n\n## ", 1)[0]
    assert flags == (
        "The delivery changes the client's own files: the Owner side reviews these changes.\n"
        "\n"
        "- `CONTRACT.md`\n"
        "\n"
        "Acceptance test files that no criterion names (they never run):\n"
        "\n"
        "- `tests/extra.py`\n"
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
