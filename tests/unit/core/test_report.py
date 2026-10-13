"""The evidence report: complete, masked, and inert where the delivery chose the text."""

from __future__ import annotations

from typing import Any

from tests.unit.core.test_gate import contract
from zephyros.core.gate import compute_gate
from zephyros.core.masking import MASK
from zephyros.core.ports.delivery import Delivery
from zephyros.core.ports.verification import Outcome
from zephyros.core.report import Cost, ReportInput, mask, render

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


def test_both_kinds_of_flags_read_together() -> None:
    rendered = render(
        make(flagged_paths=("CONTRACT.md",), unnamed_acceptance_files=("tests/extra.py",))
    )
    assert "The delivery changes the client's own files" in rendered
    assert "- `CONTRACT.md`" in rendered
    assert "Acceptance test files that no criterion names (they never run):" in rendered
    assert "- `tests/extra.py`" in rendered
