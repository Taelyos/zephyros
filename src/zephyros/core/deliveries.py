"""The delivery loop: staging area, acceptance tests, verification, gate, report, pull request.

The steps of annex "Delivery steps", at the light process level:

1. `prepare`: the default branch of the project's repository becomes the base of its staging area.
2. The Owner side puts its acceptance tests (`put_acceptance`); the Contractor never writes them.
3. The Contractor pushes a branch (`push`) and declares it (`declare`).
4. The worker verifies a declared delivery: it refuses one that touches an acceptance test, builds
   the tree to verify (the delivery, with every acceptance test overlaid from the hub), runs the
   kit of the project in the verification runner, computes the gate, writes the evidence report,
   and for an accepted delivery pushes the branch to the forge and opens a pull request with the
   report.

Nothing here waits on the thread of a caller: preparing and verifying are jobs, done one after the
other by `step`, which an adapter calls on a thread of its own (like the instance manager).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from collections import deque
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol

from zephyros.core.audit import Action, AuditEvent, AuditLog, Outcome, Reason
from zephyros.core.clock import Clock, format_time
from zephyros.core.contract import ContractService
from zephyros.core.errors import Refusal
from zephyros.core.gate import (
    JUDGED_TYPES,
    Decision,
    Inspected,
    compute_gate,
    every_acceptance_test,
)
from zephyros.core.ports.delivery import Delivery, DeliveryArea, DeliveryError
from zephyros.core.ports.forge import Forge, ForgeError, PullRequestRequest, RepositoryRef
from zephyros.core.ports.verification import (
    Outcome as TestOutcome,
)
from zephyros.core.ports.verification import (
    VerificationError,
    VerificationRequest,
    VerificationRunner,
)
from zephyros.core.principals import OPERATOR
from zephyros.core.projects import ProjectService
from zephyros.core.report import Cost, ReportInput, mask, render, with_inspections

SYSTEM_PRINCIPAL = "hub"
MAX_ACCEPTANCE_FILE = 64 * 1024
ACCEPTANCE_PATH = re.compile(r"^(?!.*\.\.)[A-Za-z0-9_][A-Za-z0-9_./-]{0,150}$")


class DeliveryState(StrEnum):
    RECEIVED = "received"
    DECLARED = "declared"
    REFUSED = "refused"
    """The checks before verification refused it; nothing ran."""
    DECIDED = "decided"
    """The gate decided; no pull request (FAIL, or no forge)."""
    PROPOSED = "proposed"
    """Accepted, and a pull request carries it."""
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Source:
    repository: str
    branch: str
    state: str
    """preparing, ready or failed."""
    base: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    project_id: str
    delivery_id: str
    branch: str
    state: DeliveryState
    received_at: str
    changed_paths: tuple[str, ...] = ()
    decision: str | None = None
    outcomes: Mapping[str, str] = field(default_factory=dict)
    report: str | None = None
    pull_request: str | None = None
    detail: str | None = None
    pull_number: int | None = None
    """The number of its pull request on the forge, to comment there."""
    contract_version: int | None = None
    """The version of the contract the gate judged the delivery against."""
    inspections: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    """By object: the inspection the Owner side recorded (verdict, observation, by, at)."""
    review: Mapping[str, str] | None = None
    """The verdict of the Owner side on the delivery (verdict, reasons, by, at)."""
    flagged_paths: tuple[str, ...] = ()
    """The protected paths the delivery changes: flagged, never refused (ADR 0020)."""


class DeliveryStore(Protocol):
    def put_record(self, record: DeliveryRecord) -> None: ...

    def get_record(self, project_id: str, delivery_id: str) -> DeliveryRecord | None: ...

    def records(self, project_id: str) -> list[DeliveryRecord]: ...

    def put_acceptance(self, project_id: str, path: str, content: bytes) -> None: ...

    def acceptance(self, project_id: str) -> dict[str, bytes]: ...

    def put_source(self, project_id: str, source: Source) -> None: ...

    def get_source(self, project_id: str) -> Source | None: ...


def flagged_paths(changed: tuple[str, ...], protected: tuple[str, ...]) -> tuple[str, ...]:
    """The changed paths that are the client's own files: a protected path itself, or anything
    under a protected directory (one ending with /)."""
    return tuple(
        path
        for path in changed
        if any(path == p or (p.endswith("/") and path.startswith(p)) for p in protected)
    )


class Verdict(StrEnum):
    """The verdict of the Owner side on a delivery: recorded and posted, never a merge."""

    ACCEPT = "accept"
    ACCEPT_WITH_RESERVATIONS = "accept_with_reservations"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class _Job:
    kind: str
    project_id: str
    delivery_id: str = ""
    body: str = ""


def _run_async[R](coroutine: Coroutine[Any, Any, R]) -> R:
    return asyncio.run(coroutine)


class DeliveryService:
    def __init__(
        self,
        area: DeliveryArea,
        runner: VerificationRunner,
        forge: Forge | None,
        contracts: ContractService,
        projects: ProjectService,
        store: DeliveryStore,
        audit: AuditLog,
        clock: Clock,
        *,
        kit: str = "tests",
        timeout_seconds: int = 300,
        protected_paths: tuple[str, ...] = ("CONTRACT.md",),
        version: str = "Zephyros",
        secrets: Callable[[], list[str]] | None = None,
        run_async: Callable[[Coroutine[Any, Any, Any]], Any] = _run_async,
        events: Callable[[str, Mapping[str, object]], None] | None = None,
        rejections_alert: int | None = None,
        simulation: bool = False,
    ) -> None:
        self._area = area
        self._runner = runner
        self._forge = forge
        self._contracts = contracts
        self._projects = projects
        self._store = store
        self._audit = audit
        self._clock = clock
        self._kit = kit
        self._timeout = timeout_seconds
        self._protected_paths = protected_paths
        self._version = version
        self._secrets = secrets or list
        self._run_async = run_async
        self._events = events or (lambda _event, _payload: None)
        self._rejections_alert = rejections_alert
        self._simulation = simulation
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._jobs: deque[_Job] = deque()

    # ------------------------------------------------------------------ requests
    def prepare(self, project_id: str, repository: str, branch: str) -> Source:
        """Ask for the base of the staging area to be taken from the forge."""
        self._projects.get(project_id)
        if self._forge is None:
            raise Refusal(Reason.INVALID_REQUEST, "no forge is configured")
        try:
            RepositoryRef(repository)
        except ValueError as error:
            raise Refusal(Reason.INVALID_REQUEST, str(error)) from None
        source = Source(repository, branch, "preparing")
        self._store.put_source(project_id, source)
        self._enqueue(_Job("prepare", project_id))
        return source

    def source(self, project_id: str) -> Source | None:
        return self._store.get_source(project_id)

    def put_acceptance(self, project_id: str, path: str, content: str) -> str:
        """Keep an acceptance test of the Owner side. Returns the digest of its content."""
        self._projects.get(project_id)
        if not ACCEPTANCE_PATH.fullmatch(path):
            raise Refusal(Reason.INVALID_REQUEST, "the path is not a plain relative path")
        data = content.encode("utf-8")
        if len(data) > MAX_ACCEPTANCE_FILE:
            raise Refusal(Reason.INVALID_REQUEST, "the file is too large")
        self._store.put_acceptance(project_id, path, data)
        return hashlib.sha256(data).hexdigest()[:12]

    def base_package(self, project_id: str) -> bytes:
        try:
            return self._area.base_package(project_id)
        except DeliveryError as error:
            raise Refusal(Reason.NOT_FOUND, str(error)) from None

    def delivery_package(self, project_id: str, delivery_id: str) -> bytes:
        """A kept delivery, as a package: what the Owner side reads before judging it."""
        self._record(project_id, delivery_id)
        try:
            return self._area.package(project_id, delivery_id)
        except DeliveryError as error:
            raise Refusal(Reason.NOT_FOUND, str(error)) from None

    def push(self, project_id: str, package: bytes, branch: str) -> DeliveryRecord:
        try:
            delivery = self._area.receive(project_id, package, branch)
        except DeliveryError as error:
            raise Refusal(Reason.INVALID_REQUEST, str(error)) from None
        record = DeliveryRecord(
            project_id,
            delivery.delivery_id,
            delivery.branch,
            DeliveryState.RECEIVED,
            format_time(self._clock.now()),
            delivery.changed_paths,
        )
        self._store.put_record(record)
        return record

    def declare(self, project_id: str, delivery_id: str) -> DeliveryRecord:
        record = self._record(project_id, delivery_id)
        if record.state is not DeliveryState.RECEIVED:
            raise Refusal(Reason.NOT_PENDING, f"the delivery is already {record.state.value}")
        declared = replace(record, state=DeliveryState.DECLARED)
        self._store.put_record(declared)
        self._enqueue(_Job("verify", project_id, delivery_id))
        return declared

    def status(self, project_id: str, delivery_id: str) -> DeliveryRecord:
        return self._record(project_id, delivery_id)

    def record_inspection(
        self,
        by: str,
        project_id: str,
        delivery_id: str,
        object_id: str,
        verdict: Inspected,
        observation: str,
    ) -> DeliveryRecord:
        """Record the Owner side's inspection of one requirement or constraint of a decided
        delivery, and decide again from the same results (ADR 0020). A delivery that failed
        stays failed: an inspection only evidences what tests cannot show."""
        record = self._decided(project_id, delivery_id)
        version = self._contracts.read(OPERATOR, project_id, record.contract_version or 0)
        item = version.objects.get(object_id)
        if item is None or item.get("type") not in JUDGED_TYPES:
            raise Refusal(Reason.NOT_FOUND, f"{object_id} is no requirement or constraint here")
        raw = item.get("methods") or ()
        methods = [str(m) for m in raw] if isinstance(raw, (list, tuple)) else []
        if not [m for m in methods if m != "test"]:
            raise Refusal(
                Reason.INVALID_REQUEST, f"{object_id} is verified by tests only: nothing to inspect"
            )
        inspections = {key: dict(value) for key, value in record.inspections.items()}
        inspections[object_id] = {
            "verdict": verdict.value,
            "observation": observation,
            "by": by,
            "at": format_time(self._clock.now()),
        }
        gate = compute_gate(
            version.objects,
            {test: TestOutcome(value) for test, value in record.outcomes.items()},
            inspections={key: Inspected(value["verdict"]) for key, value in inspections.items()},
        )
        updated = replace(
            record,
            inspections=inspections,
            decision=gate.decision.value,
            report=with_inspections(
                record.report or "", inspections, record.decision, gate, self._secrets()
            ),
        )
        self._store.put_record(updated)
        self._record_event(
            project_id,
            Action.GATE_DECISION,
            Outcome.OK,
            delivery=delivery_id,
            decision=gate.decision.value,
            previous=record.decision,
            contract_version=record.contract_version,
            after="inspection",
        )
        if gate.decision.value != record.decision:
            self._events(
                "delivery.decided",
                {
                    "project_id": project_id,
                    "delivery_id": delivery_id,
                    "branch": record.branch,
                    "decision": gate.decision.value,
                },
            )
        self._comment_later(
            updated,
            f"**Inspection recorded** by {by}: {object_id} "
            f"{'conforms' if verdict is Inspected.CONFORMS else 'does not conform'}.\n\n"
            f"{observation}\n\n**Decision now: {gate.decision.value}** (it was "
            f"{record.decision}), computed by the gate from the same results and the "
            "inspections recorded.",
        )
        return updated

    def record_review(
        self, by: str, project_id: str, delivery_id: str, verdict: Verdict, reasons: str
    ) -> DeliveryRecord:
        """Record the verdict of the Owner side on a decided delivery, with its reasons; it is
        posted on the pull request. The merge stays a human act (ADR 0020)."""
        record = self._decided(project_id, delivery_id)
        review = {
            "verdict": verdict.value,
            "reasons": reasons,
            "by": by,
            "at": format_time(self._clock.now()),
        }
        updated = replace(record, review=review)
        self._store.put_record(updated)
        self._events(
            "delivery.reviewed",
            {
                "project_id": project_id,
                "delivery_id": delivery_id,
                "branch": record.branch,
                "decision": record.decision,
                "verdict": verdict.value,
                "reasons": mask(reasons, self._secrets()),
            },
        )
        self._comment_later(
            updated,
            f"**Review of the Owner side** ({by}): **{verdict.value.replace('_', ' ')}**, "
            f"on a gate decision {record.decision}.\n\n{reasons}\n\n"
            "The merge stays a human decision.",
        )
        return updated

    def _decided(self, project_id: str, delivery_id: str) -> DeliveryRecord:
        record = self._record(project_id, delivery_id)
        if record.contract_version is None or record.state not in (
            DeliveryState.DECIDED,
            DeliveryState.PROPOSED,
        ):
            raise Refusal(Reason.NOT_PENDING, "the gate has not decided this delivery yet")
        return record

    def _comment_later(self, record: DeliveryRecord, body: str) -> None:
        """Post a comment on the pull request of the delivery, from the worker: a forge call
        never runs on a request."""
        if record.pull_number is not None and self._forge is not None:
            self._enqueue(_Job("comment", record.project_id, record.delivery_id, body))

    def _do_comment(self, job: _Job) -> None:
        record = self._store.get_record(job.project_id, job.delivery_id)
        source = self._store.get_source(job.project_id)
        if record is None or record.pull_number is None or source is None or self._forge is None:
            return
        try:
            self._run_async(
                self._forge.comment(
                    RepositoryRef(source.repository),
                    record.pull_number,
                    mask(job.body, self._secrets()),
                )
            )
        except ForgeError as error:
            self._record_event(
                job.project_id,
                Action.PULL_REQUEST_COMMENT,
                Outcome.ERROR,
                delivery=job.delivery_id,
                error=str(error),
            )
            return
        self._record_event(
            job.project_id,
            Action.PULL_REQUEST_COMMENT,
            Outcome.OK,
            delivery=job.delivery_id,
            number=record.pull_number,
        )

    def _record(self, project_id: str, delivery_id: str) -> DeliveryRecord:
        record = self._store.get_record(project_id, delivery_id)
        if record is None:
            raise Refusal(Reason.NOT_FOUND, "there is no such delivery")
        return record

    # ------------------------------------------------------------------ the worker
    def _enqueue(self, job: _Job) -> None:
        with self._wake:
            self._jobs.append(job)
            self._wake.notify()

    def step(self) -> bool:
        with self._lock:
            if not self._jobs:
                return False
            job = self._jobs.popleft()
        if job.kind == "prepare":
            self._do_prepare(job.project_id)
        elif job.kind == "comment":
            self._do_comment(job)
        else:
            self._do_verify(job.project_id, job.delivery_id)
        return True

    def wait(self, timeout: float) -> None:
        with self._wake:
            if not self._jobs:
                self._wake.wait(timeout)

    def wake(self) -> None:
        with self._wake:
            self._wake.notify_all()

    def tick(self) -> None:
        """Nothing to watch between jobs."""

    def shutdown(self) -> None:
        """Nothing runs outside a job."""

    # ------------------------------------------------------------------ prepare
    def _do_prepare(self, project_id: str) -> None:
        source = self._store.get_source(project_id)
        if source is None or self._forge is None:
            return
        forge = self._forge
        try:
            package = self._run_async(forge.fetch(RepositoryRef(source.repository), source.branch))
            base = self._area.prepare(project_id, package, source.branch)
        except (ForgeError, DeliveryError) as error:
            self._store.put_source(project_id, replace(source, state="failed", detail=str(error)))
            self._record_event(project_id, Action.STAGING_PREPARE, Outcome.ERROR, error=str(error))
            return
        self._store.put_source(project_id, replace(source, state="ready", base=base))
        self._record_event(
            project_id,
            Action.STAGING_PREPARE,
            Outcome.OK,
            repository=source.repository,
            branch=source.branch,
            base=base[:12],
        )
        self._events(
            "staging.prepared",
            {
                "project_id": project_id,
                "repository": source.repository,
                "branch": source.branch,
                "base": base[:12],
            },
        )

    # ------------------------------------------------------------------ verify
    def _do_verify(self, project_id: str, delivery_id: str) -> None:
        record = self._record(project_id, delivery_id)
        try:
            self._verify(record)
        except Exception as error:  # reason: a job must not stop the worker
            detail = (
                str(error)
                if isinstance(error, (VerificationError, DeliveryError))
                else type(error).__name__
            )
            self._store.put_record(replace(record, state=DeliveryState.ERROR, detail=detail))
            self._record_event(
                project_id,
                Action.VERIFICATION_RUN,
                Outcome.ERROR,
                delivery=delivery_id,
                error=detail,
            )

    def _verify(self, record: DeliveryRecord) -> None:
        project_id = record.project_id
        delivery = self._area.get(project_id, record.delivery_id)
        version = self._contracts.read(OPERATOR, project_id)
        objects = version.objects
        tests = every_acceptance_test(objects)
        base_files = dict(self._area.tree(project_id, "base").files)
        acceptance = self._store.acceptance(project_id)
        protected = {t.partition("::")[0] for t in tests} & set(base_files) | set(acceptance)
        touched = sorted(set(delivery.changed_paths) & protected)
        if touched:
            detail = "the delivery changes acceptance tests: " + ", ".join(touched[:10])
            self._store.put_record(replace(record, state=DeliveryState.REFUSED, detail=detail))
            self._record_event(
                project_id,
                Action.DELIVERY_CHECK,
                Outcome.REFUSED,
                Reason.PROTECTED_PATH,
                delivery=record.delivery_id,
                paths=len(touched),
            )
            return
        flagged = flagged_paths(delivery.changed_paths, self._protected_paths)
        if flagged:
            self._record_event(
                project_id,
                Action.DELIVERY_CHECK,
                Outcome.OK,
                delivery=record.delivery_id,
                flagged=len(flagged),
            )
        files = dict(self._area.tree(project_id, delivery.delivery_id).files)
        for path in protected:
            if path in acceptance:
                files[path] = acceptance[path]
            elif path in base_files:
                files[path] = base_files[path]
        # Which version of each acceptance test ran: the Owner side may put a new one later.
        ran = tuple(
            (path, hashlib.sha256(acceptance[path]).hexdigest()[:12])
            for path in sorted(protected & set(acceptance))
        )
        # An acceptance test of the Owner side that no criterion names never runs: say so.
        unnamed = tuple(
            sorted(path for path in acceptance if path not in {t.partition("::")[0] for t in tests})
        )
        outcomes: dict[str, Any] = {}
        log = ""
        if tests:
            result = self._runner.run(
                VerificationRequest(project_id, self._kit, files, tests, self._timeout)
            )
            outcomes = dict(result.outcomes)
            log = result.log
        self._record_event(
            project_id,
            Action.VERIFICATION_RUN,
            Outcome.OK,
            delivery=record.delivery_id,
            kit=self._kit,
            tests=len(tests),
            passed=sum(1 for o in outcomes.values() if o.value == "passed"),
        )
        gate = compute_gate(objects, outcomes)
        self._record_event(
            project_id,
            Action.GATE_DECISION,
            Outcome.OK,
            delivery=record.delivery_id,
            decision=gate.decision.value,
            contract_version=version.number,
        )
        project = self._projects.get(project_id)
        report = render(
            ReportInput(
                project_name=project.name,
                delivery=delivery,
                contract_version=version.number,
                contract_hash=version.content_hash,
                kit=self._kit,
                gate=gate,
                outcomes=outcomes,
                log=log,
                cost=self._cost(project_id),
                produced_at=format_time(self._clock.now()),
                produced_by=self._version,
                simulation=self._simulation,
                flagged_paths=flagged,
                acceptance_files=ran,
                unnamed_acceptance_files=unnamed,
            ),
            secrets=self._secrets(),
        )
        decided = replace(
            record,
            state=DeliveryState.DECIDED,
            decision=gate.decision.value,
            outcomes={k: v.value for k, v in outcomes.items()},
            report=report,
            contract_version=version.number,
            flagged_paths=flagged,
        )
        self._store.put_record(decided)
        if gate.decision is not Decision.FAIL:
            self._propose(decided, delivery, report)
        self._events(
            "delivery.decided",
            {
                "project_id": record.project_id,
                "delivery_id": record.delivery_id,
                "branch": record.branch,
                "decision": gate.decision.value,
            },
        )
        if gate.decision is Decision.FAIL and self._rejections_alert is not None:
            # EXG-23: the loop the Owner and the Contractor may fall into — a streak of
            # refused deliveries that a pass breaks. Counted per project, not per branch:
            # the areas take one delivery per branch, so a branch cannot refuse twice.
            streak = 0
            for found in reversed(self._store.records(record.project_id)):
                if found.decision != Decision.FAIL.value:
                    break
                streak += 1
            if streak >= self._rejections_alert:
                self._events(
                    "delivery.loop",
                    {
                        "project_id": record.project_id,
                        "branch": record.branch,
                        "rejections": streak,
                    },
                )

    def _propose(self, record: DeliveryRecord, delivery: Delivery, report: str) -> None:
        source = self._store.get_source(record.project_id)
        if self._forge is None or source is None:
            return
        forge = self._forge
        repository = RepositoryRef(source.repository)
        head = f"zephyros/{record.delivery_id}-{record.branch}"
        title = f"{record.branch} (delivery {record.delivery_id}, {record.decision})"
        try:
            package = self._area.package(record.project_id, delivery.delivery_id)
            self._run_async(forge.push(repository, head, package))
            opened = self._run_async(
                forge.open_pull_request(
                    PullRequestRequest(repository, head, source.branch, title, report)
                )
            )
        except (ForgeError, DeliveryError) as error:
            self._store.put_record(replace(record, detail=f"no pull request: {error}"))
            self._record_event(
                record.project_id,
                Action.PULL_REQUEST_OPEN,
                Outcome.ERROR,
                delivery=record.delivery_id,
                error=str(error),
            )
            return
        self._store.put_record(
            replace(
                record,
                state=DeliveryState.PROPOSED,
                pull_request=opened.url,
                pull_number=opened.number,
            )
        )
        self._record_event(
            record.project_id,
            Action.PULL_REQUEST_OPEN,
            Outcome.OK,
            delivery=record.delivery_id,
            number=opened.number,
        )

    def _cost(self, project_id: str) -> Cost:
        calls = prompt = completion = 0
        for item in self._audit.read(project_id, 0, 100_000):
            if item.action is Action.GATEWAY_CALL and item.outcome is Outcome.OK:
                parameters = json.loads(item.parameters)
                calls += 1
                prompt += int(parameters.get("prompt_tokens", 0))
                completion += int(parameters.get("completion_tokens", 0))
        return Cost(calls, prompt, completion)

    def _record_event(
        self,
        project_id: str,
        action: Action,
        outcome: Outcome,
        reason: Reason = Reason.NONE,
        **parameters: object,
    ) -> None:
        self._audit.append(
            AuditEvent(project_id, SYSTEM_PRINCIPAL, action, outcome, reason, parameters)
        )
