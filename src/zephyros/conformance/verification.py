"""Conformance suite of the verification runner interface.

To check an implementation, subclass `VerificationConformance` and implement `make_fixture`: a
runner, one of its kits, and a function that writes files holding acceptance tests with the
given expected behavior ("pass", "fail" or "error"), in that kit's convention.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from zephyros.conformance._support import ConformanceError, check, check_interface_version
from zephyros.core.ports.verification import (
    INTERFACE_VERSION,
    Outcome,
    VerificationError,
    VerificationRequest,
    VerificationRunner,
)


@dataclass(frozen=True)
class VerificationFixture:
    runner: VerificationRunner
    kit: str
    write: Callable[[Mapping[str, str]], tuple[dict[str, bytes], dict[str, str]]]
    """`write({label: behavior})` returns the files and, for each label, the test identifier."""


class VerificationConformance:
    """Rules every verification runner follows."""

    def make_fixture(self) -> VerificationFixture:
        raise NotImplementedError

    def _run(
        self, behaviors: Mapping[str, str], extra_tests: tuple[str, ...] = ()
    ) -> tuple[dict[str, Outcome], dict[str, str]]:
        fixture = self.make_fixture()
        files, names = fixture.write(behaviors)
        request = VerificationRequest(
            "p_conformance", fixture.kit, files, (*names.values(), *extra_tests)
        )
        result = fixture.runner.run(request)
        return dict(result.outcomes), names

    def test_follows_the_tested_major_version(self) -> None:
        check_interface_version(self.make_fixture().runner.interface_version, INTERFACE_VERSION)

    def test_names_the_kit_it_runs(self) -> None:
        fixture = self.make_fixture()
        check(fixture.kit in fixture.runner.kits, "the runner does not name its kit")

    def test_tells_a_passing_test_from_a_failing_one(self) -> None:
        outcomes, names = self._run({"good": "pass", "bad": "fail"})
        check(outcomes[names["good"]] is Outcome.PASSED, "a passing test did not pass")
        check(outcomes[names["bad"]] is Outcome.FAILED, "a failing test did not fail")

    def test_a_broken_test_never_passes(self) -> None:
        outcomes, names = self._run({"broken": "error"})
        check(
            outcomes[names["broken"]] in (Outcome.ERROR, Outcome.FAILED),
            "a broken test passed or was not run",
        )

    def test_gives_one_outcome_to_each_test_asked_for_and_to_no_other(self) -> None:
        fixture = self.make_fixture()
        files, names = fixture.write({"good": "pass", "unasked": "pass"})
        missing = "absent/test_missing.py::test_missing"
        request = VerificationRequest("p_conformance", fixture.kit, files, (names["good"], missing))
        result = fixture.runner.run(request)
        outcomes = dict(result.outcomes)
        check(set(outcomes) == {names["good"], missing}, f"outcomes for {sorted(outcomes)}")
        check(outcomes[missing] in (Outcome.NOT_RUN, Outcome.ERROR), "an absent test passed")
        # Deselected tests are well-formed and never shadow a test asked for (audit A2).
        check(
            all(isinstance(test, str) for test in result.deselected),
            "deselected is not a tuple of test identifiers",
        )
        check(
            not set(result.deselected) & set(outcomes),
            "a test asked for was also reported as deselected",
        )

    def test_gives_the_same_outcomes_twice(self) -> None:
        fixture = self.make_fixture()
        files, names = fixture.write({"good": "pass", "bad": "fail"})
        request = VerificationRequest("p_conformance", fixture.kit, files, tuple(names.values()))
        first = dict(fixture.runner.run(request).outcomes)
        second = dict(fixture.runner.run(request).outcomes)
        check(first == second, "two runs of the same tests differ")

    def test_refuses_a_kit_it_does_not_run(self) -> None:
        fixture = self.make_fixture()
        files, names = fixture.write({"good": "pass"})
        request = VerificationRequest("p_conformance", "no-such-kit", files, tuple(names.values()))
        try:
            fixture.runner.run(request)
        except VerificationError:
            return
        raise ConformanceError("an unknown kit was run")
