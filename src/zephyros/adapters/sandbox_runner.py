"""The verification runner of the hub: each verification in a sandbox, through the filter.

It packs the files to verify and the manifest into a tar and sends it to the filter, which runs
the kit image in a sandbox of its own and destroys it afterwards (ADR 0017, ADR 0004).

Result integrity is the kit's job (ADR 0017): it keeps delivered code out of the judging process
(an import barrier, black-box acceptance tests) so there is no in-process code left to forge the
outcomes. The canary tests this runner still plants — two tests of the hub that must fail — are a
cheap backstop, not the defence: they catch a run that believed a *blanket* forgery (everything
marked passed) or a kit that did not run at all, by refusing to trust a run whose canary came back
anything but failed. A run stopped at its time limit is not believed either: it verified nothing.
"""

from __future__ import annotations

import io
import json
import secrets
import tarfile
from collections.abc import Iterable, Mapping

from zephyros.adapters.filter.client import FilterClient
from zephyros.adapters.filter.sandbox import MANIFEST
from zephyros.core.broker import BrokerError
from zephyros.core.ports.verification import (
    INTERFACE_VERSION,
    Outcome,
    VerificationError,
    VerificationRequest,
    VerificationResult,
)

CANARIES = 2
"""How many tests of the hub ride in every run; each must come back failed."""


def pack(request: VerificationRequest) -> bytes:
    if MANIFEST in request.files:
        raise VerificationError(f"a file is named {MANIFEST}, which the sandbox reserves")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:") as archive:
        manifest = json.dumps(
            {
                "project_id": request.project_id,
                "kit": request.kit,
                "tests": list(request.tests),
                "timeout_seconds": request.timeout_seconds,
            }
        ).encode()
        for name, data in [(MANIFEST, manifest), *sorted(request.files.items())]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _with_canaries(request: VerificationRequest) -> tuple[VerificationRequest, tuple[str, ...]]:
    """The request plus two tests of the hub that must fail, and their identifiers."""
    files = dict(request.files)
    canaries: list[str] = []
    for _ in range(CANARIES):
        token = secrets.token_hex(4)
        path = f"test_zephyros_canary_{token}.py"
        files[path] = (
            f"def test_zephyros_canary_{token}():\n"
            '    assert False, "a canary of the hub: this test must fail"\n'
        ).encode()
        canaries.append(f"{path}::test_zephyros_canary_{token}")
    asked = VerificationRequest(
        request.project_id,
        request.kit,
        files,
        (*request.tests, *canaries),
        request.timeout_seconds,
    )
    return asked, tuple(canaries)


def _check_canaries(canaries: tuple[str, ...], outcomes: Mapping[str, Outcome]) -> None:
    missed = [test for test in canaries if outcomes.get(test) is not Outcome.FAILED]
    if missed:
        raise VerificationError(
            "the results of the run cannot be trusted: a canary test of the hub did not fail"
        )


class SandboxRunner:
    def __init__(self, client: FilterClient, kits: Iterable[str]) -> None:
        self._client = client
        self._kits = frozenset(kits)

    @property
    def interface_version(self) -> str:
        return INTERFACE_VERSION

    @property
    def kits(self) -> frozenset[str]:
        return self._kits

    def run(self, request: VerificationRequest) -> VerificationResult:
        if request.kit not in self._kits:
            raise VerificationError(f"this runner does not run the kit {request.kit!r}")
        asked, canaries = _with_canaries(request)
        try:
            answer = self._client.run_sandbox(pack(asked), request.timeout_seconds)
        except BrokerError as error:
            raise VerificationError(f"the sandbox did not run: {error}") from None
        raw = answer.get("outcomes", {})
        outcomes = {
            test: Outcome(raw.get(test, "not_run"))
            if raw.get(test) in {o.value for o in Outcome}
            else Outcome.NOT_RUN
            for test in asked.tests
        }
        deselected = tuple(t for t in answer.get("deselected", []) if isinstance(t, str))
        log = str(answer.get("log", ""))
        timed_out = bool(answer.get("timed_out"))
        if timed_out:
            log += "\n[the run reached its time limit and was stopped]"
            # ATK-22: a run stopped at its limit proves nothing — nothing was verified, and a
            # results line printed before the kill is not believed either, deselected included.
            outcomes = dict.fromkeys(asked.tests, Outcome.NOT_RUN)
            deselected = ()
        else:
            _check_canaries(canaries, outcomes)
        mine = {test: outcomes[test] for test in request.tests}
        return VerificationResult(
            mine, log, {}, float(answer.get("duration_seconds", 0.0)), deselected
        )
