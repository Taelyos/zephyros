"""The gate: the acceptance decision, by fixed rules, from the recorded results (EXG-09, ADR 0005).

No model takes part. The same contract and the same results give the same decision, whatever a
delivery says about itself.

1. Each requirement is judged by its own methods. A requirement whose methods are only T (test)
   is verified when it has at least one acceptance test and every one of them passed. Its
   acceptance tests are the `test` of the criteria of the stories that cover it. A method other
   than T needs a judgment recorded by the Owner side; until one exists, the requirement is not
   verified, and says why.
2. The rules count the verified requirements: every P0, at least 90 % of the P1, and at least
   80 % of all of them by default (the contract may set other values later).
3. FAIL when a rule is not met; CONCERNS when they are all met and there are reservations; PASS
   otherwise. WAIVED is never computed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction

from zephyros.core.ports.verification import Outcome

Objects = Mapping[str, Mapping[str, object]]


class Decision(StrEnum):
    PASS = "PASS"  # noqa: S105  # reason: a decision, not a password
    CONCERNS = "CONCERNS"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class Rules:
    """Minimum shares of verified requirements, in percent."""

    p0: int = 100
    p1: int = 90
    overall: int = 80


@dataclass(frozen=True, slots=True)
class Judged:
    """One requirement, as the gate saw it."""

    id: str
    priority: str
    methods: tuple[str, ...]
    stories: tuple[str, ...]
    tests: tuple[tuple[str, Outcome], ...]
    verified: bool
    why: str


@dataclass(frozen=True, slots=True)
class RuleCheck:
    name: str
    required_percent: int
    verified: int
    total: int

    @property
    def met(self) -> bool:
        if self.total == 0:
            return True
        return Fraction(self.verified, self.total) * 100 >= self.required_percent


@dataclass(frozen=True, slots=True)
class GateResult:
    decision: Decision
    requirements: tuple[Judged, ...]
    checks: tuple[RuleCheck, ...]
    reservations: tuple[str, ...]


def covering_stories(objects: Objects) -> dict[str, tuple[str, ...]]:
    """For each requirement, the stories that cover it, in a stable order (EXG-11)."""
    stories_of: dict[str, set[str]] = {}
    for item in objects.values():
        if item.get("type") == "link" and item.get("kind") == "covers":
            stories_of.setdefault(str(item["target"]), set()).add(str(item["source"]))
    return {
        key: tuple(sorted(stories_of.get(key, set())))
        for key, item in objects.items()
        if item.get("type") == "requirement"
    }


def acceptance_tests(objects: Objects) -> dict[str, tuple[str, ...]]:
    """For each requirement, its acceptance tests: the `test` of each criterion of each story
    that covers it, in a stable order."""
    tests_of_story: dict[str, set[str]] = {}
    for item in objects.values():
        if item.get("type") == "criterion" and item.get("test"):
            tests_of_story.setdefault(str(item["story"]), set()).add(str(item["test"]))
    found: dict[str, tuple[str, ...]] = {}
    for key, stories in covering_stories(objects).items():
        tests: set[str] = set()
        for story in stories:
            tests |= tests_of_story.get(story, set())
        found[key] = tuple(sorted(tests))
    return found


def every_acceptance_test(objects: Objects) -> tuple[str, ...]:
    return tuple(sorted({t for tests in acceptance_tests(objects).values() for t in tests}))


def _judge(
    key: str,
    item: Mapping[str, object],
    tests: tuple[str, ...],
    outcomes: Mapping[str, Outcome],
    stories: tuple[str, ...],
) -> Judged:
    raw = item.get("methods") or ()
    methods = tuple(str(m) for m in raw) if isinstance(raw, (list, tuple)) else ()
    priority = str(item.get("priority", ""))
    results = tuple((test, outcomes.get(test, Outcome.NOT_RUN)) for test in tests)
    others = [m for m in methods if m != "test"]
    if "test" not in methods:
        why = "no method of this requirement is computed by the hub yet"
        verified = False
    elif not tests:
        why = "no acceptance test is linked to it"
        verified = False
    elif any(outcome is not Outcome.PASSED for _, outcome in results):
        why = "an acceptance test did not pass"
        verified = False
    elif others:
        why = f"the tests passed; {', '.join(others)} needs a recorded judgment"
        verified = False
    else:
        why = "every acceptance test passed"
        verified = True
    return Judged(key, priority, methods, stories, results, verified, why)


def compute_gate(
    objects: Objects,
    outcomes: Mapping[str, Outcome],
    reservations: Sequence[str] = (),
    rules: Rules = Rules(),  # noqa: B008  # reason: a frozen dataclass, never mutated
) -> GateResult:
    """The decision for a delivery: the contract it was verified against, the outcome of each
    acceptance test, and the reservations of the Owner side."""
    tests = acceptance_tests(objects)
    stories = covering_stories(objects)
    judged = tuple(
        _judge(key, objects[key], tests[key], outcomes, stories.get(key, ()))
        for key in sorted(tests)
    )

    def count(name: str, required: int, chosen: list[Judged]) -> RuleCheck:
        return RuleCheck(name, required, sum(j.verified for j in chosen), len(chosen))

    checks = (
        count("P0 requirements verified", rules.p0, [j for j in judged if j.priority == "P0"]),
        count("P1 requirements verified", rules.p1, [j for j in judged if j.priority == "P1"]),
        count("requirements verified", rules.overall, list(judged)),
    )
    if not all(check.met for check in checks):
        decision = Decision.FAIL
    elif reservations:
        decision = Decision.CONCERNS
    else:
        decision = Decision.PASS
    return GateResult(decision, judged, checks, tuple(reservations))
