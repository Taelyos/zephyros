"""A verification runner that runs no code: its one kit, "assertions", reads the answer.

A test `path::name` passes when the file `path` holds the line `name: pass`, fails with
`name: fail`, is an error with any other value, and is not run when the file or the line is
absent. Deterministic and harmless: it stands in for a real runner in tests.
"""

from __future__ import annotations

from zephyros.core.ports.verification import (
    INTERFACE_VERSION,
    Outcome,
    VerificationError,
    VerificationRequest,
    VerificationResult,
)

KIT = "assertions"


class AssertionRunner:
    @property
    def interface_version(self) -> str:
        return INTERFACE_VERSION

    @property
    def kits(self) -> frozenset[str]:
        return frozenset({KIT})

    def run(self, request: VerificationRequest) -> VerificationResult:
        if request.kit != KIT:
            raise VerificationError(f"this runner does not run the kit {request.kit!r}")
        outcomes = {test: self._outcome(request, test) for test in request.tests}
        log = "\n".join(f"{test} {outcome.value}" for test, outcome in outcomes.items())
        return VerificationResult(outcomes, log)

    @staticmethod
    def _outcome(request: VerificationRequest, test: str) -> Outcome:
        path, _, name = test.partition("::")
        content = request.files.get(path)
        if content is None or not name:
            return Outcome.NOT_RUN
        for line in content.decode("utf-8", errors="replace").splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() == name:
                return {"pass": Outcome.PASSED, "fail": Outcome.FAILED}.get(
                    value.strip(), Outcome.ERROR
                )
        return Outcome.NOT_RUN
