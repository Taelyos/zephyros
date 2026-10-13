"""The gate: the acceptance decision, by fixed rules, from the recorded results (EXG-09, ADR 0005).

No model takes part. The same contract and the same results give the same decision, whatever a
delivery says about itself.

1. Each requirement and each constraint is judged by its own methods (ADR 0020: the constraints
   count too). The test method is evidenced by acceptance tests: the `test` of the criteria of
   the stories that cover the object, every one of which must pass. Any other method
   (inspection, analysis, demonstration) is evidenced by an inspection recorded by the Owner
   side for this delivery: one that found the object conforming verifies it, one that found it
   not conforming does not.
2. The rules count the verified objects: every P0, at least 90 % of the P1, and at least 80 % of
   all of them by default (the contract may set other values later).
3. An object whose only shortfall is a judgment not recorded yet *awaits* it: it is a
   reservation, never a pass. FAIL when a rule is not met even counting the awaited objects as
   verified; CONCERNS when the rules are met only with them, or when there are reservations;
   PASS otherwise. WAIVED is never computed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
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


class Inspected(StrEnum):
    """The verdict of an inspection recorded by the Owner side for one object of one delivery."""

    CONFORMS = "conforms"
    DOES_NOT_CONFORM = "does_not_conform"


JUDGED_TYPES = ("requirement", "constraint")


@dataclass(frozen=True, slots=True)
class Judged:
    """One requirement or constraint, as the gate saw it."""

    id: str
    priority: str
    methods: tuple[str, ...]
    stories: tuple[str, ...]
    tests: tuple[tuple[str, Outcome], ...]
    verified: bool
    why: str
    awaiting: bool = False
    """Its only shortfall is a judgment that is not recorded yet."""


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
    """For each requirement and constraint, the stories that cover it, in a stable order
    (EXG-11)."""
    stories_of: dict[str, set[str]] = {}
    for item in objects.values():
        if item.get("type") == "link" and item.get("kind") == "covers":
            stories_of.setdefault(str(item["target"]), set()).add(str(item["source"]))
    return {
        key: tuple(sorted(stories_of.get(key, set())))
        for key, item in objects.items()
        if item.get("type") in JUDGED_TYPES
    }


def acceptance_tests(objects: Objects) -> dict[str, tuple[str, ...]]:
    """For each requirement and constraint, its acceptance tests: the `test` of each criterion
    of each story that covers it, in a stable order."""
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
    inspection: Inspected | None,
) -> Judged:
    raw = item.get("methods") or ()
    methods = tuple(str(m) for m in raw) if isinstance(raw, (list, tuple)) else ()
    priority = str(item.get("priority", ""))
    results = tuple((test, outcome_of(outcomes, test)) for test in tests)
    others = [m for m in methods if m != "test"]
    tested = "test" in methods

    def judged(verified: bool, why: str, *, awaiting: bool = False) -> Judged:
        return Judged(key, priority, methods, stories, results, verified, why, awaiting)

    if not methods:
        return judged(False, "it names no verification method")
    if tested and not tests:
        return judged(False, "no acceptance test is linked to it")
    if tested and any(outcome is not Outcome.PASSED for _, outcome in results):
        return judged(False, "an acceptance test did not pass")
    passed = "the tests passed; " if tested else ""
    if not others:
        return judged(True, "every acceptance test passed")
    named = ", ".join(others)
    if inspection is None:
        return judged(False, f"{passed}{named} awaits a recorded judgment", awaiting=True)
    if inspection is Inspected.CONFORMS:
        return judged(True, f"{passed}the recorded {named} found it conforming")
    return judged(False, f"{passed}the recorded {named} found it not conforming")


def outcome_of(outcomes: Mapping[str, Outcome], test: str) -> Outcome:
    return outcomes.get(test, Outcome.NOT_RUN)


def compute_gate(
    objects: Objects,
    outcomes: Mapping[str, Outcome],
    reservations: Sequence[str] = (),
    rules: Rules = Rules(),  # noqa: B008  # reason: a frozen dataclass, never mutated
    inspections: Mapping[str, Inspected] | None = None,
) -> GateResult:
    """The decision for a delivery: the contract it was verified against, the outcome of each
    acceptance test, the reservations of the Owner side, and the inspections it recorded."""
    found = inspections or {}
    tests = acceptance_tests(objects)
    stories = covering_stories(objects)
    judged = tuple(
        _judge(key, objects[key], tests[key], outcomes, stories.get(key, ()), found.get(key))
        for key in sorted(tests)
    )

    def checks_of(verified: Callable[[Judged], bool]) -> tuple[RuleCheck, ...]:
        def count(name: str, required: int, chosen: list[Judged]) -> RuleCheck:
            return RuleCheck(name, required, sum(verified(j) for j in chosen), len(chosen))

        return (
            count(
                "P0 requirements and constraints verified",
                rules.p0,
                [j for j in judged if j.priority == "P0"],
            ),
            count(
                "P1 requirements and constraints verified",
                rules.p1,
                [j for j in judged if j.priority == "P1"],
            ),
            count("requirements and constraints verified", rules.overall, list(judged)),
        )

    checks = checks_of(lambda j: j.verified)
    awaited = [j for j in judged if j.awaiting]
    every = tuple(reservations) + tuple(
        f"{j.id} ({j.priority}) awaits a recorded judgment: "
        + ", ".join(m for m in j.methods if m != "test")
        for j in awaited
    )
    if not all(check.met for check in checks_of(lambda j: j.verified or j.awaiting)):
        decision = Decision.FAIL
    elif every:
        decision = Decision.CONCERNS
    else:
        decision = Decision.PASS
    return GateResult(decision, judged, checks, every)
