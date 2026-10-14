"""The verification runner interface: start the project under verification and observe it.

A runner runs one verification kit (ADR 0017): how to start the project and how to observe it.
It is given the files to verify and the acceptance tests to run, and returns one outcome per
test with bounded evidence. It renders no decision: the gate does (ADR 0005).

Version 0.x. 0.2.0 adds `VerificationResult.deselected`: the tests the kit found in the asked
files but did not run because no acceptance criterion named them, so the report can say which
acceptance tests never ran.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

INTERFACE_VERSION = "0.2.0"
MAX_LOG = 20_000


class VerificationError(Exception):
    """The runner could not run the verification: an unknown kit, or a failure of its own."""


class Outcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    """The test could not run to an end: it crashed, or its setup failed."""
    NOT_RUN = "not_run"
    """The test was asked for and did not run: absent, filtered out, or out of time."""


@dataclass(frozen=True, slots=True)
class VerificationRequest:
    project_id: str
    kit: str
    files: Mapping[str, bytes]
    tests: tuple[str, ...]
    """The acceptance tests to run, as the kit names them (`path::name` for pytest)."""
    timeout_seconds: int = 300

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))
        object.__setattr__(self, "tests", tuple(self.tests))
        if not self.tests:
            raise ValueError("a verification runs at least one test")
        if len(set(self.tests)) != len(self.tests):
            raise ValueError("a test is named twice")
        if self.timeout_seconds <= 0:
            raise ValueError("the timeout is positive")


@dataclass(frozen=True, slots=True)
class VerificationResult:
    outcomes: Mapping[str, Outcome]
    log: str = ""
    """What the kit printed, cut to MAX_LOG characters."""
    evidence: Mapping[str, bytes] = field(default_factory=dict)
    """Files the kit produced as evidence (screenshots), by name."""
    duration_seconds: float = 0.0
    deselected: tuple[str, ...] = ()
    """Tests the kit found in the asked files but did not run, because no criterion named them
    (`path::name`). None of them is a test asked for. The report lists them as never run."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcomes", MappingProxyType(dict(self.outcomes)))
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))
        object.__setattr__(self, "log", self.log[:MAX_LOG])
        object.__setattr__(self, "deselected", tuple(self.deselected))


class VerificationRunner(Protocol):
    @property
    def interface_version(self) -> str: ...

    @property
    def kits(self) -> frozenset[str]:
        """The kits this runner can run."""
        ...

    def run(self, request: VerificationRequest) -> VerificationResult:
        """Run the acceptance tests of the request with its kit. Every test asked for gets
        exactly one outcome, and no other test gets one. `deselected` names tests the kit saw in
        the asked files but did not run for want of a criterion; none of them is a test asked
        for. Raises VerificationError for a kit it does not run."""
        ...
