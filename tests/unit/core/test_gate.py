"""The gate: requirements judged by their methods, then fixed rules (EXG-09)."""

from __future__ import annotations

import random
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from zephyros.core.gate import (
    NOTHING_JUDGED,
    NOTHING_PASSED,
    NOTHING_TESTED,
    Decision,
    Inspected,
    Rules,
    acceptance_tests,
    compute_gate,
    covering_stories,
)
from zephyros.core.ports.verification import Outcome

PASS, FAIL, ERROR, NOT_RUN = Outcome.PASSED, Outcome.FAILED, Outcome.ERROR, Outcome.NOT_RUN


def requirement(
    n: int, priority: str = "P1", methods: tuple[str, ...] = ("test",)
) -> dict[str, Any]:
    return {
        "type": "requirement",
        "id": f"EXG-{n:02d}",
        "text": "The system shall.",
        "priority": priority,
        "methods": list(methods),
    }


def contract(
    *specs: tuple[int, str, tuple[str, ...], tuple[str, ...]],
) -> dict[str, dict[str, Any]]:
    """Each spec: (number, priority, methods, tests). One story and one criterion per test."""
    objects: dict[str, dict[str, Any]] = {}
    for n, priority, methods, tests in specs:
        req = requirement(n, priority, methods)
        objects[req["id"]] = req
        story = f"US-E1-{n:02d}"
        objects[story] = {"type": "story", "id": story, "epic": "E1", "title": "s", "text": "t"}
        link = f"LNK:{story}>{req['id']}"
        objects[link] = {
            "type": "link",
            "id": link,
            "kind": "covers",
            "source": story,
            "target": req["id"],
        }
        for k, test in enumerate(tests, start=1):
            cid = f"{story}.{k}"
            objects[cid] = {
                "type": "criterion",
                "id": cid,
                "story": story,
                "text": "c",
                "test": test,
            }
    return objects


def test_the_acceptance_tests_of_a_requirement_come_from_the_criteria_of_its_stories() -> None:
    objects = contract((1, "P0", ("test",), ("t.py::a", "t.py::b")), (2, "P1", ("test",), ()))
    objects["US-E1-01.9"] = {
        "type": "criterion",
        "id": "US-E1-01.9",
        "story": "US-E1-01",
        "text": "no test",
    }
    assert acceptance_tests(objects) == {"EXG-01": ("t.py::a", "t.py::b"), "EXG-02": ()}


@pytest.mark.parametrize(
    ("outcomes", "verified", "why"),
    [
        ({"t.py::a": PASS, "t.py::b": PASS}, True, "every acceptance test passed"),
        ({"t.py::a": PASS, "t.py::b": FAIL}, False, "an acceptance test did not pass"),
        ({"t.py::a": PASS, "t.py::b": ERROR}, False, "an acceptance test did not pass"),
        ({"t.py::a": PASS}, False, "an acceptance test did not pass"),
    ],
)
def test_a_requirement_tested_is_verified_only_when_every_test_passed(
    outcomes: dict[str, Outcome], verified: bool, why: str
) -> None:
    result = compute_gate(contract((1, "P0", ("test",), ("t.py::a", "t.py::b"))), outcomes)
    judged = result.requirements[0]
    assert (judged.verified, judged.why) == (verified, why)
    assert dict(judged.tests).get("t.py::b", NOT_RUN) == outcomes.get("t.py::b", NOT_RUN)


def test_a_requirement_without_tests_or_with_another_method_is_not_verified() -> None:
    objects = contract(
        (1, "P1", ("test",), ()),
        (2, "P1", ("inspection",), ("t.py::x",)),
        (3, "P1", ("test", "analysis"), ("t.py::y",)),
    )
    result = compute_gate(objects, {"t.py::x": PASS, "t.py::y": PASS})
    whys = {j.id: (j.verified, j.why) for j in result.requirements}
    assert whys["EXG-01"] == (False, "no acceptance test is linked to it")
    assert whys["EXG-02"] == (False, "inspection awaits a recorded judgment")
    assert whys["EXG-03"] == (False, "the tests passed; analysis awaits a recorded judgment")


def constraint(n: int, priority: str, methods: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "constraint",
        "id": f"CTR-{n:02d}",
        "text": "The service shall.",
        "priority": priority,
        "methods": list(methods),
    }


def test_a_constraint_is_judged_like_a_requirement() -> None:
    """ADR 0020: CTR-01 (P0, inspection) had no evidence in three deliveries that passed."""
    objects = contract((1, "P0", ("test",), ("t.py::x",)))
    objects["CTR-01"] = constraint(1, "P0", ("inspection",))
    result = compute_gate(objects, {"t.py::x": PASS})
    assert [j.id for j in result.requirements] == ["CTR-01", "EXG-01"]
    assert result.decision is Decision.CONCERNS
    assert result.reservations == ("CTR-01 (P0) awaits a recorded judgment: inspection",)
    assert (result.checks[0].verified, result.checks[0].total) == (1, 2)


def test_a_recorded_inspection_decides_the_object_it_judges() -> None:
    objects = contract((1, "P0", ("test",), ("t.py::x",)))
    objects["CTR-01"] = constraint(1, "P0", ("inspection",))
    conforming = compute_gate(
        objects, {"t.py::x": PASS}, inspections={"CTR-01": Inspected.CONFORMS}
    )
    assert conforming.decision is Decision.PASS
    assert {j.id: j.why for j in conforming.requirements}["CTR-01"] == (
        "the recorded inspection found it conforming"
    )
    refused = compute_gate(
        objects, {"t.py::x": PASS}, inspections={"CTR-01": Inspected.DOES_NOT_CONFORM}
    )
    assert refused.decision is Decision.FAIL
    assert {j.id: j.why for j in refused.requirements}["CTR-01"] == (
        "the recorded inspection found it not conforming"
    )


def test_an_awaited_judgment_never_rescues_a_failing_delivery() -> None:
    objects = contract((1, "P0", ("test",), ("t.py::x",)))
    objects["CTR-01"] = constraint(1, "P0", ("inspection",))
    assert compute_gate(objects, {"t.py::x": FAIL}).decision is Decision.FAIL


def test_an_awaited_p1_judgment_is_a_reservation_even_when_the_rules_are_met() -> None:
    """Nine P1 of ten meet the 90 % rule; the tenth, awaiting its inspection, still holds PASS."""
    specs = [(n, "P1", ("test",), (f"t.py::t{n}",)) for n in range(1, 10)]
    objects = contract(*specs, (10, "P1", ("inspection",), ()))
    result = compute_gate(objects, {f"t.py::t{n}": PASS for n in range(1, 10)})
    assert all(check.met for check in result.checks)
    assert result.decision is Decision.CONCERNS


def test_an_object_that_names_no_method_is_not_verified() -> None:
    objects = contract((1, "P1", (), ()))
    [judged] = compute_gate(objects, {}).requirements
    assert (judged.verified, judged.why) == (False, "it names no verification method")


def test_every_p0_must_be_verified() -> None:
    objects = contract((1, "P0", ("test",), ("a::x",)), (2, "P0", ("test",), ("a::y",)))
    assert compute_gate(objects, {"a::x": PASS, "a::y": PASS}).decision is Decision.PASS
    failed = compute_gate(objects, {"a::x": PASS, "a::y": FAIL})
    assert failed.decision is Decision.FAIL
    assert [c.met for c in failed.checks] == [False, True, False]


@pytest.mark.parametrize(("passing", "decision"), [(10, "PASS"), (9, "PASS"), (8, "FAIL")])
def test_ninety_percent_of_the_p1(passing: int, decision: str) -> None:
    objects = contract(*[(n, "P1", ("test",), (f"a::t{n}",)) for n in range(1, 11)])
    outcomes = {f"a::t{n}": PASS if n <= passing else FAIL for n in range(1, 11)}
    assert compute_gate(objects, outcomes).decision.value == decision


def test_eighty_percent_overall_counts_the_p2_too() -> None:
    specs = [(1, "P0", ("test",), ("a::p0",))] + [
        (n, "P2", ("test",), (f"a::t{n}",)) for n in range(2, 6)
    ]
    objects = contract(*specs)
    good = {"a::p0": PASS, "a::t2": PASS, "a::t3": PASS, "a::t4": PASS, "a::t5": FAIL}
    assert compute_gate(objects, good).decision is Decision.PASS  # 4 of 5
    worse = {**good, "a::t4": FAIL}
    assert compute_gate(objects, worse).decision is Decision.FAIL  # 3 of 5


def test_reservations_turn_a_pass_into_concerns_and_never_a_fail_into_anything() -> None:
    objects = contract((1, "P0", ("test",), ("a::x",)))
    assert compute_gate(objects, {"a::x": PASS}, ["slow"]).decision is Decision.CONCERNS
    assert compute_gate(objects, {"a::x": FAIL}, ["slow"]).decision is Decision.FAIL


def test_the_rules_can_be_stricter() -> None:
    objects = contract(*[(n, "P1", ("test",), (f"a::t{n}",)) for n in range(1, 11)])
    outcomes = {f"a::t{n}": PASS if n <= 9 else FAIL for n in range(1, 11)}
    assert compute_gate(objects, outcomes, rules=Rules(p1=100)).decision is Decision.FAIL


def test_a_contract_with_nothing_to_judge_never_passes_and_says_so() -> None:
    """T10: a rule with nothing to count is met, yet nothing was judged."""
    result = compute_gate({}, {})
    assert all(c.total == 0 and c.met for c in result.checks)
    assert result.decision is Decision.CONCERNS
    assert result.reservations == (NOTHING_JUDGED,)


@pytest.mark.parametrize(
    ("outcome", "decision", "reservations"),
    [
        (NOT_RUN, Decision.CONCERNS, (NOTHING_TESTED,)),
        (FAIL, Decision.CONCERNS, (NOTHING_PASSED,)),
        (ERROR, Decision.CONCERNS, (NOTHING_PASSED,)),
        (PASS, Decision.PASS, ()),
    ],
)
def test_a_delivery_where_no_acceptance_test_passed_never_passes(
    outcome: Outcome, decision: Decision, reservations: tuple[str, ...]
) -> None:
    """T10: nine P2 objects verified by inspection meet every rule, and a tenth is tested.
    Unless its test passed, nothing the hub ran supports a PASS; and a test that ran and
    failed never does better than one that did not run."""
    objects = contract(
        *[(n, "P2", ("inspection",), ()) for n in range(1, 10)],
        (10, "P2", ("test",), ("t.py::t10",)),
    )
    inspections = {f"EXG-{n:02d}": Inspected.CONFORMS for n in range(1, 10)}
    result = compute_gate(objects, {"t.py::t10": outcome}, inspections=inspections)
    assert all(check.met for check in result.checks)
    assert (result.decision, result.reservations) == (decision, reservations)


def test_a_contract_judged_by_inspection_alone_is_concerns_at_best() -> None:
    """T10: the hub ran nothing; the inspections alone do not make a PASS."""
    objects = {"CTR-01": constraint(1, "P0", ("inspection",))}
    result = compute_gate(objects, {}, inspections={"CTR-01": Inspected.CONFORMS})
    assert [j.verified for j in result.requirements] == [True]
    assert (result.decision, result.reservations) == (Decision.CONCERNS, (NOTHING_TESTED,))


specs_strategy = st.lists(
    st.tuples(
        st.sampled_from(["P0", "P1", "P2"]),
        st.lists(st.sampled_from([PASS, FAIL, ERROR, NOT_RUN]), min_size=0, max_size=3),
    ),
    min_size=0,
    max_size=8,
)


def _build(specs: list[tuple[str, list[Outcome]]]) -> tuple[dict[str, Any], dict[str, Outcome]]:
    objects = contract(
        *[
            (n, priority, ("test",), tuple(f"a::r{n}t{k}" for k in range(len(results))))
            for n, (priority, results) in enumerate(specs, start=1)
        ]
    )
    outcomes = {
        f"a::r{n}t{k}": outcome
        for n, (_p, results) in enumerate(specs, start=1)
        for k, outcome in enumerate(results)
    }
    return objects, outcomes


@given(specs=specs_strategy, seed=st.integers(0, 1000))
def test_the_decision_does_not_depend_on_the_order_of_the_objects(
    specs: list[tuple[str, list[Outcome]]], seed: int
) -> None:
    objects, outcomes = _build(specs)
    keys = list(objects)
    random.Random(seed).shuffle(keys)  # noqa: S311  # reason: an order for a test, not a secret
    shuffled = {k: objects[k] for k in keys}
    assert compute_gate(objects, outcomes) == compute_gate(shuffled, outcomes)


RANK = {Decision.FAIL: 0, Decision.CONCERNS: 1, Decision.PASS: 2}


@given(specs=specs_strategy, which=st.integers(0, 100))
def test_making_a_test_pass_never_makes_the_decision_worse(
    specs: list[tuple[str, list[Outcome]]], which: int
) -> None:
    objects, outcomes = _build(specs)
    if not outcomes:
        return
    test = sorted(outcomes)[which % len(outcomes)]
    better = {**outcomes, test: PASS}
    assert (
        RANK[compute_gate(objects, better).decision]
        >= RANK[compute_gate(objects, outcomes).decision]
    )


def test_each_requirement_carries_the_stories_that_cover_it() -> None:
    """EXG-11: the traceability goes all the way to the judgment the report renders."""
    objects = contract((1, "P0", ("test",), ("tests/t.py::test_add",)))
    assert covering_stories(objects) == {"EXG-01": ("US-E1-01",)}
    judged = compute_gate(objects, {"tests/t.py::test_add": PASS}).requirements[0]
    assert judged.stories == ("US-E1-01",)


def test_a_requirement_no_story_covers_says_so() -> None:
    objects = contract((1, "P0", ("test",), ("tests/t.py::test_add",)))
    objects["EXG-09"] = {
        "type": "requirement",
        "id": "EXG-09",
        "text": "The system shall.",
        "priority": "P1",
        "methods": ["test"],
    }
    assert covering_stories(objects)["EXG-09"] == ()
    judged = next(j for j in compute_gate(objects, {}).requirements if j.id == "EXG-09")
    assert (judged.stories, judged.tests, judged.verified) == ((), (), False)
    assert judged.why == "no acceptance test is linked to it"
