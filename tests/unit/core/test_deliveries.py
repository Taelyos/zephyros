"""The delivery service when something goes wrong: no forge, a forge or a runner that fails."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

import pytest

from tests.unit.core.test_gate import contract
from zephyros.adapters.dummy.delivery import InMemoryDeliveryArea, make_package, state_id
from zephyros.adapters.dummy.forge import InMemoryForge
from zephyros.adapters.dummy.verification import AssertionRunner
from zephyros.adapters.memory.clock import FakeClock
from zephyros.adapters.memory.contract_store import InMemoryContractStore
from zephyros.adapters.memory.delivery_store import InMemoryDeliveryStore
from zephyros.adapters.memory.log_store import InMemoryLogStore
from zephyros.adapters.memory.project_store import InMemoryProjectStore
from zephyros.core.audit import Action, AuditEvent, AuditLog, AuditRecord, LogError, Outcome, Reason
from zephyros.core.contract import ContractService, parse_change
from zephyros.core.deliveries import (
    WRITE_ATTEMPTS,
    DeliveryRecord,
    DeliveryService,
    DeliveryState,
    StaleRecordError,
    Verdict,
    deselected_tests,
    flagged_paths,
    report_for,
)
from zephyros.core.errors import Refusal
from zephyros.core.gate import NOTHING_JUDGED, Inspected
from zephyros.core.ports.agent_engine import Side
from zephyros.core.ports.forge import (
    ForgeError,
    PullRequest,
    PullRequestRequest,
    RepositoryRef,
)
from zephyros.core.ports.verification import Outcome as VerificationOutcome
from zephyros.core.ports.verification import (
    VerificationError,
    VerificationRequest,
    VerificationResult,
)
from zephyros.core.principals import MAIN_AGENT, OPERATOR, instance_principal
from zephyros.core.projects import ProjectService
from zephyros.core.tools import ToolsProbe

BASE = {"calc.py": b"x = 1\n", "checks/add.txt": b"test_add: pass\n"}


class DeselectingRunner(AssertionRunner):
    """The assertions runner, answering like a kit of the interface 0.2.0: every other test line
    of an asked file is a test it saw and did not run."""

    def run(self, request: VerificationRequest) -> VerificationResult:
        result = super().run(request)
        asked = set(request.tests)
        seen = [
            f"{path}::{line.partition(':')[0].strip()}"
            for path in sorted({test.partition("::")[0] for test in asked})
            for line in request.files.get(path, b"").decode(errors="replace").splitlines()
            if ":" in line
        ]
        return VerificationResult(
            result.outcomes, result.log, deselected=tuple(t for t in seen if t not in asked)
        )


class Exploding(AssertionRunner):
    def run(self, request: VerificationRequest) -> VerificationResult:
        del request
        raise VerificationError("the runner fell over")


class Failing(AssertionRunner):
    """A runner whose tests fail: the gate says FAIL without touching a protected path."""

    def run(self, request: VerificationRequest) -> VerificationResult:
        return VerificationResult(
            dict.fromkeys(request.tests, VerificationOutcome.FAILED),
            "the tests failed",
            {},
            0.0,
        )


class Buggy(AssertionRunner):
    def run(self, request: VerificationRequest) -> VerificationResult:
        del request
        raise RuntimeError("a bug, with a secret inside: sk-should-not-appear")


class RefusingForge(InMemoryForge):
    async def push(self, repository: RepositoryRef, branch: str, package: bytes) -> None:
        del repository, branch, package
        raise ForgeError("the forge is read-only today")


ADD_ONLY = ((1, "P0", ("test",), ("checks/add.txt::test_add",)),)
"""EXG-01 (P0), verified by checks/add.txt::test_add, which the base holds."""
ADD_AND_SUB = (*ADD_ONLY, (2, "P1", ("test",), ("checks/sub.txt::test_sub",)))
"""And EXG-02 (P1), verified by checks/sub.txt::test_sub, which nobody holds yet."""


def service(
    runner: Any = None,
    forge: Any = "default",
    specs: tuple[tuple[int, str, tuple[str, ...], tuple[str, ...]], ...] = ADD_ONLY,
    store: InMemoryDeliveryStore | None = None,
    events: Callable[[str, Mapping[str, object]], None] | None = None,
    log_store: InMemoryLogStore | None = None,
    secrets: Callable[[], list[str]] | None = None,
    rejections_alert: int | None = None,
    tools: ToolsProbe | None = None,
    simulation: bool = False,
) -> tuple[DeliveryService, str, AuditLog, InMemoryDeliveryStore]:
    """A delivery service on a project with a contract; `events` hears what the service tells
    the hub."""
    clock = FakeClock()
    audit = AuditLog(log_store or InMemoryLogStore(), clock)
    projects = ProjectService(InMemoryProjectStore(), clock, new_id=lambda: "p_service1")
    contracts = ContractService(InMemoryContractStore(), clock, new_change_id=lambda: "c_00000001")
    project = projects.create(OPERATOR, {"name": "Calc"}).id
    objects = contract(*specs)
    if objects:
        change = parse_change(
            {"summary": "s", "operations": [{"op": "put", "object": o} for o in objects.values()]}
        )
        contracts.apply_reserved(OPERATOR, project, change)
    if forge == "default":
        forge = InMemoryForge(
            {"example/calc": frozenset({"main"})},
            {("example/calc", "main"): make_package(BASE)},
        )
    store = store or InMemoryDeliveryStore()
    made = DeliveryService(
        InMemoryDeliveryArea(),
        runner or AssertionRunner(),
        forge,
        contracts,
        projects,
        store,
        audit,
        clock,
        kit="assertions",
        events=events,
        secrets=secrets,
        rejections_alert=rejections_alert,
        tools=tools,
        simulation=simulation,
    )
    return made, project, audit, store


def ready(made: DeliveryService, project: str) -> None:
    made.prepare(project, "example/calc", "main")
    while made.step():
        pass


def delivered(made: DeliveryService, project: str, files: dict[str, bytes] | None = None) -> str:
    package = make_package(files or {**BASE, "calc.py": b"x = 2\n"}, state_id(BASE))
    record = made.push(project, package, "work")
    made.declare(project, record.delivery_id)
    while made.step():
        pass
    return record.delivery_id


def test_without_a_forge_there_is_nothing_to_prepare() -> None:
    made, project, _audit, _store = service(forge=None)
    with pytest.raises(Refusal, match="no forge"):
        made.prepare(project, "example/calc", "main")


def test_a_repository_name_that_is_not_one_is_refused() -> None:
    made, project, _audit, _store = service()
    with pytest.raises(Refusal):
        made.prepare(project, "not a repository", "main")


def test_a_branch_the_forge_does_not_have_leaves_the_source_failed() -> None:
    made, project, audit, _store = service()
    made.prepare(project, "example/calc", "nope")
    assert made.step()
    source = made.source(project)
    assert source is not None
    assert (source.state, "unknown branch" in str(source.detail)) == ("failed", True)
    assert audit.read(project)[-1].outcome is Outcome.ERROR


def test_acceptance_files_are_plain_paths_of_bounded_size() -> None:
    made, project, _audit, store = service()
    for bad in ("../x", "/abs", "a/../b", ""):
        with pytest.raises(Refusal):
            made.put_acceptance(project, bad, "x")
    with pytest.raises(Refusal, match="too large"):
        made.put_acceptance(project, "checks/big.txt", "x" * (64 * 1024 + 1))
    assert len(made.put_acceptance(project, "checks/ok.txt", "test_ok: pass\n")) == 12
    assert store.acceptance(project) == {"checks/ok.txt": b"test_ok: pass\n"}


def test_a_project_without_a_staging_area_has_no_base_and_takes_no_push() -> None:
    made, project, _audit, _store = service()
    with pytest.raises(Refusal):
        made.base_package(project)
    with pytest.raises(Refusal):
        made.push(project, make_package(BASE), "work")


def test_an_unknown_delivery_cannot_be_declared_or_read() -> None:
    made, project, _audit, _store = service()
    with pytest.raises(Refusal) as caught:
        made.declare(project, "d9")
    assert caught.value.reason is Reason.NOT_FOUND
    with pytest.raises(Refusal):
        made.status(project, "d9")


def test_a_runner_that_fails_puts_the_delivery_in_error_with_its_reason() -> None:
    made, project, audit, _store = service(runner=Exploding())
    ready(made, project)
    record = made.status(project, delivered(made, project))
    assert (record.state, record.detail) == (DeliveryState.ERROR, "the runner fell over")
    assert audit.read(project)[-1].action is Action.VERIFICATION_RUN


def test_a_bug_in_a_runner_is_reported_by_its_kind_only() -> None:
    made, project, _audit, _store = service(runner=Buggy())
    ready(made, project)
    record = made.status(project, delivered(made, project))
    assert (record.state, record.detail) == (DeliveryState.ERROR, "RuntimeError")


def test_a_forge_that_refuses_the_branch_leaves_a_decision_without_a_pull_request() -> None:
    forge = RefusingForge(
        {"example/calc": frozenset({"main"})}, {("example/calc", "main"): make_package(BASE)}
    )
    made, project, _audit, _store = service(forge=forge)
    ready(made, project)
    record = made.status(project, delivered(made, project))
    assert (record.state, record.decision) == (DeliveryState.DECIDED, "PASS")
    assert record.pull_request is None
    assert "read-only" in str(record.detail)


def test_the_cost_in_the_report_is_the_gateway_usage_of_the_project() -> None:
    made, project, audit, _store = service()
    for tokens in (100, 250):
        audit.append(
            AuditEvent(
                project,
                f"contractor:{project}",
                Action.GATEWAY_CALL,
                Outcome.OK,
                parameters={"prompt_tokens": tokens, "completion_tokens": 10},
            )
        )
    ready(made, project)
    report = made.status(project, delivered(made, project)).report
    assert report is not None
    assert "2 model calls, 350 prompt tokens, 20 completion tokens." in report


def test_the_report_names_the_engines_of_the_contractor_turns_before_the_delivery() -> None:
    """ADR 0029: read from the starts of turn in the log, up to the push of the delivery. The
    Owner's turns, and those after the push, did not build it."""
    made, project, audit, _store = service()
    ready(made, project)

    def turn(side: str, **engine: str) -> None:
        audit.append(
            AuditEvent(
                project, "hub", Action.TURN_START, Outcome.OK, parameters={"side": side, **engine}
            )
        )

    opencode = {"engine": "opencode", "engine_version": "1.19.0"}
    turn("contractor")  # recorded before the hub recorded engines
    turn("owner", engine="hermes", engine_version="0.21.6 (818c13be)")
    turn("contractor", **opencode)
    turn("contractor", **opencode)
    record = made.push(project, make_package({**BASE, "calc.py": b"x = 1\n"}, state_id(BASE)), "w")
    audit.append(
        AuditEvent(
            project,
            f"contractor:{project}",
            Action.DELIVERY_PUSH,
            Outcome.OK,
            parameters={"delivery_id": record.delivery_id},
        )
    )
    turn("contractor", engine="hermes", engine_version="0.21.6 (818c13be)")
    made.declare(project, record.delivery_id)
    while made.step():
        pass
    report = made.status(project, record.delivery_id).report
    assert report is not None
    assert (
        "- Built by the agent of the Contractor: `not recorded` (1 turn), "
        "`opencode 1.19.0` (2 turns)"
    ) in report.splitlines()


def test_a_report_without_a_contractor_turn_says_so() -> None:
    made, project, _audit, _store = service()
    ready(made, project)
    report = made.status(project, delivered(made, project)).report
    assert report is not None
    assert "- Built by the agent of the Contractor: no turn of the Contractor recorded" in report


def test_a_delivery_declared_before_the_contract_runs_nothing_and_never_passes() -> None:
    """T10: it passed for want of anything to judge. The runner would fail if it ran."""
    made, project, _audit, _store = service(runner=Exploding(), specs=())
    ready(made, project)
    record = made.status(project, delivered(made, project))
    assert (record.outcomes, record.decision, record.contract_version) == ({}, "CONCERNS", 0)
    assert record.report is not None
    assert f"- {NOTHING_JUDGED}" in record.report.splitlines()


def test_a_hub_that_starts_again_runs_the_verifications_it_had_not_ended() -> None:
    """The queue of the loop is in memory: what a stopped hub had declared and not decided runs
    again when it starts (resume), and nothing else does."""
    made, project, _audit, store = service()
    ready(made, project)
    decided = delivered(made, project)
    package = make_package({**BASE, "calc.py": b"x = 2\n"}, state_id(BASE))
    waiting = made.push(project, package, "later").delivery_id
    made.declare(project, waiting)  # the hub stops here: its queue goes with it
    # The hub that starts again: the same store, staging area and services, a new queue.
    again = DeliveryService(
        made._area,
        made._runner,
        made._forge,
        made._contracts,
        made._projects,
        store,
        made._audit,
        made._clock,
        kit="assertions",
    )
    assert not again.step()
    assert again.resume() == [(project, waiting)]
    while again.step():
        pass
    assert again.status(project, waiting).decision == "PASS"
    assert again.resume() == []
    assert store.get_record(project, decided) == made.status(project, decided)


def test_the_worker_hooks_are_harmless() -> None:
    made, _project, _audit, _store = service()
    assert not made.step()
    made.tick()
    made.shutdown()
    made.wake()
    made.wait(0.01)


def delivered_on(made: DeliveryService, project: str, branch: str, files: dict[str, bytes]) -> str:
    """A delivery on a named branch (the areas take one delivery per branch)."""
    package = make_package({**BASE, **files}, state_id(BASE))
    record = made.push(project, package, branch)
    made.declare(project, record.delivery_id)
    while made.step():
        pass
    return record.delivery_id


def test_three_refused_deliveries_on_a_branch_alert_the_hub() -> None:
    """EXG-23: a branch that keeps coming back refused is a loop — after the configured
    number of refusals the hub says so itself."""
    events: list[tuple[str, Mapping[str, object]]] = []
    clock = FakeClock()
    audit = AuditLog(InMemoryLogStore(), clock)
    projects = ProjectService(InMemoryProjectStore(), clock, new_id=lambda: "p_service1")
    contracts = ContractService(InMemoryContractStore(), clock, new_change_id=lambda: "c_00000001")
    project = projects.create(OPERATOR, {"name": "Calc"}).id
    objects = contract((1, "P0", ("test",), ("checks/add.txt::test_add",)))
    change = parse_change(
        {"summary": "s", "operations": [{"op": "put", "object": o} for o in objects.values()]}
    )
    contracts.apply_reserved(OPERATOR, project, change)
    forge = InMemoryForge(
        {"example/calc": frozenset({"main"})},
        {("example/calc", "main"): make_package(BASE)},
    )
    made = DeliveryService(
        InMemoryDeliveryArea(),
        Failing(),
        forge,
        contracts,
        projects,
        InMemoryDeliveryStore(),
        audit,
        clock,
        kit="assertions",
        rejections_alert=3,
        events=lambda event, payload: events.append((event, payload)),
    )
    ready(made, project)

    def retry(attempt: int) -> None:
        # each retry brings a change, and the acceptance tests fail every time
        delivered_on(made, project, f"work{attempt}", {"calc.py": f"x = {attempt}\n".encode()})

    retry(1)
    retry(2)
    assert [event for event, _payload in events if event == "delivery.loop"] == []
    retry(3)
    loops = [payload for event, payload in events if event == "delivery.loop"]
    assert len(loops) == 1
    assert loops[0]["branch"] == "work3"
    assert loops[0]["rejections"] == 3


def test_a_pass_breaks_the_loop_and_the_count_starts_over() -> None:
    events: list[tuple[str, Mapping[str, object]]] = []
    clock = FakeClock()
    audit = AuditLog(InMemoryLogStore(), clock)
    projects = ProjectService(InMemoryProjectStore(), clock, new_id=lambda: "p_service1")
    contracts = ContractService(InMemoryContractStore(), clock, new_change_id=lambda: "c_00000001")
    project = projects.create(OPERATOR, {"name": "Calc"}).id
    objects = contract((1, "P0", ("test",), ("checks/add.txt::test_add",)))
    change = parse_change(
        {"summary": "s", "operations": [{"op": "put", "object": o} for o in objects.values()]}
    )
    contracts.apply_reserved(OPERATOR, project, change)
    forge = InMemoryForge(
        {"example/calc": frozenset({"main"})},
        {("example/calc", "main"): make_package(BASE)},
    )
    failing_runner = Failing()

    class Mixed(AssertionRunner):
        """Fails while the flag is set, passes otherwise."""

        def run(self, request: VerificationRequest) -> VerificationResult:
            if mixed["failing"]:
                return failing_runner.run(request)
            return super().run(request)

    mixed = {"failing": True}
    made = DeliveryService(
        InMemoryDeliveryArea(),
        Mixed(),
        forge,
        contracts,
        projects,
        InMemoryDeliveryStore(),
        audit,
        clock,
        kit="assertions",
        rejections_alert=3,
        events=lambda event, payload: events.append((event, payload)),
    )
    ready(made, project)
    for attempt in (1, 2):
        delivered_on(made, project, f"work{attempt}", {"calc.py": f"x = {attempt}\n".encode()})
    # a delivery that passes breaks the streak
    mixed["failing"] = False
    delivered_on(made, project, "work3", {"calc.py": b"x = 9\n"})
    mixed["failing"] = True
    for attempt in (4, 5):
        delivered_on(made, project, f"work{attempt}", {"calc.py": f"x = {attempt}\n".encode()})
    assert [event for event, _payload in events if event == "delivery.loop"] == []


# ----------------------------------------------------------------------------- ADR 0020
def with_constraint(made: DeliveryService, project: str) -> None:
    """CTR-01, P0, verified by inspection: what tests cannot show."""
    constraint = {
        "type": "constraint",
        "id": "CTR-01",
        "text": "The service shall use only the standard library.",
        "priority": "P0",
        "methods": ["inspection"],
    }
    change = parse_change(
        {"summary": "a constraint", "operations": [{"op": "put", "object": constraint}]}
    )
    made._contracts.apply_reserved(OPERATOR, project, change)


def test_a_constraint_awaiting_its_inspection_holds_the_delivery_at_concerns() -> None:
    made, project, _audit, _store = service()
    with_constraint(made, project)
    ready(made, project)
    record = made.status(project, delivered(made, project))
    assert record.decision == "CONCERNS"
    assert record.contract_version == 2
    assert record.state is DeliveryState.PROPOSED
    assert record.report is not None
    assert "CTR-01 (P0) awaits a recorded judgment: inspection" in record.report


def test_a_recorded_inspection_decides_again_and_the_report_says_so() -> None:
    made, project, audit, _store = service()
    with_constraint(made, project)
    ready(made, project)
    delivery = delivered(made, project)
    record = made.record_inspection(
        "owner:p_service1", project, delivery, "CTR-01", Inspected.CONFORMS, "imports: stdlib only"
    )
    assert record.decision == "PASS"
    assert record.inspections["CTR-01"]["verdict"] == "conforms"
    assert record.report is not None
    assert "## Inspections recorded after the gate" in record.report
    assert "**Decision now: PASS** (it was CONCERNS before the last inspection)" in record.report
    assert record.contractor_report is not None  # T07: the Contractor reads the addendum too
    assert record.contractor_report.endswith(str(record.addendum))
    last = audit.read(project, 0, 1000)[-1]
    assert (last.action, last.outcome) == (Action.GATE_DECISION, Outcome.OK)
    again = made.record_inspection(
        "owner:p_service1", project, delivery, "CTR-01", Inspected.DOES_NOT_CONFORM, "imports yaml"
    )
    assert again.decision == "FAIL"
    assert again.report is not None
    assert again.report.count("## Inspections recorded after the gate") == 1
    assert made.status(project, delivery).decision == "FAIL"


def test_an_inspection_needs_a_decided_delivery_and_an_object_to_inspect() -> None:
    made, project, _audit, _store = service()
    with_constraint(made, project)
    ready(made, project)
    delivery = delivered(made, project)
    with pytest.raises(Refusal) as caught:
        made.record_inspection("operator", project, delivery, "EXG-01", Inspected.CONFORMS, "x")
    assert caught.value.reason is Reason.INVALID_REQUEST  # verified by tests only
    with pytest.raises(Refusal) as caught:
        made.record_inspection("operator", project, delivery, "CTR-09", Inspected.CONFORMS, "x")
    assert caught.value.reason is Reason.NOT_FOUND
    pushed = made.push(project, make_package({**BASE, "calc.py": b"x = 3\n"}, state_id(BASE)), "w2")
    with pytest.raises(Refusal) as caught:
        made.record_inspection(
            "operator", project, pushed.delivery_id, "CTR-01", Inspected.CONFORMS, "x"
        )
    assert caught.value.reason is Reason.NOT_PENDING


def test_a_change_to_the_clients_own_files_is_flagged_never_refused() -> None:
    """ADR 0020: a Contractor of the second real run rewrote the client's CONTRACT.md."""
    made, project, _audit, _store = service()
    ready(made, project)
    delivery = delivered(made, project, {**BASE, "calc.py": b"x = 2\n", "CONTRACT.md": b"# mine\n"})
    record = made.status(project, delivery)
    assert record.flagged_paths == ("CONTRACT.md",)
    assert record.state is DeliveryState.PROPOSED
    assert record.report is not None
    assert "## Flags" in record.report
    assert "- `CONTRACT.md`" in record.report


def test_the_flagged_paths_are_the_protected_files_and_directories() -> None:
    changed = ("CONTRACT.md", "docs/CONTRACT.md", "spec/a.md", "specs.md", "app.py")
    assert flagged_paths(changed, ("CONTRACT.md", "spec/")) == ("CONTRACT.md", "spec/a.md")
    assert flagged_paths(changed, ()) == ()


def calc_forge() -> InMemoryForge:
    return InMemoryForge(
        {"example/calc": frozenset({"main"})}, {("example/calc", "main"): make_package(BASE)}
    )


def test_the_owner_sides_verdict_is_kept_logged_and_posted_on_the_pull_request() -> None:
    """ADR 0020: the second real run's verdicts lived in a scratch file."""
    forge = calc_forge()
    made, project, audit, store = service(forge=forge)
    ready(made, project)
    delivery = delivered(made, project)
    assert made.status(project, delivery).pull_number == 1
    record = made.record_review(
        "owner:p_service1", project, delivery, Verdict.ACCEPT_WITH_RESERVATIONS, "EXG-01 holds."
    )
    assert record.review is not None
    assert (record.review["verdict"], record.review["reasons"]) == (
        "accept_with_reservations",
        "EXG-01 holds.",
    )
    assert store.get_record(project, delivery) == record
    assert forge.comments == {}  # posted by the worker, never on the request
    assert made.step()
    [comment] = forge.comments[("example/calc", 1)]
    assert "**accept with reservations**, on a gate decision PASS" in comment
    assert "EXG-01 holds." in comment
    last = audit.read(project, 0, 1000)[-1]
    assert (last.action, last.outcome) == (Action.PULL_REQUEST_COMMENT, Outcome.OK)


def test_an_inspection_is_posted_on_the_pull_request_too() -> None:
    forge = calc_forge()
    made, project, _audit, _store = service(forge=forge)
    with_constraint(made, project)
    ready(made, project)
    delivery = delivered(made, project)
    made.record_inspection(
        "operator", project, delivery, "CTR-01", Inspected.CONFORMS, "stdlib only"
    )
    assert forge.comments == {}  # posted by the worker, never on the request
    assert made.step()
    [comment] = forge.comments[("example/calc", 1)]
    assert "**Inspection recorded** by operator: CTR-01 conforms." in comment
    assert "**Decision now: PASS** (it was CONCERNS)" in comment


class CommentRefusingForge(InMemoryForge):
    async def comment(self, repository: RepositoryRef, number: int, body: str) -> None:
        del repository, number, body
        raise ForgeError("comments are locked")


def test_a_comment_the_forge_refuses_is_logged_and_the_verdict_stays() -> None:
    forge = CommentRefusingForge(
        {"example/calc": frozenset({"main"})}, {("example/calc", "main"): make_package(BASE)}
    )
    made, project, audit, _store = service(forge=forge)
    ready(made, project)
    delivery = delivered(made, project)
    made.record_review("operator", project, delivery, Verdict.REJECT, "CTR-01 fails.")
    assert made.step()
    last = audit.read(project, 0, 1000)[-1]
    assert (last.action, last.outcome) == (Action.PULL_REQUEST_COMMENT, Outcome.ERROR)
    assert "comments are locked" in last.parameters
    review = made.status(project, delivery).review
    assert review is not None
    assert review["verdict"] == "reject"


def test_a_verdict_needs_a_decided_delivery_and_without_a_pull_request_stays_local() -> None:
    forge = RefusingForge(
        {"example/calc": frozenset({"main"})}, {("example/calc", "main"): make_package(BASE)}
    )
    made, project, audit, _store = service(forge=forge)
    ready(made, project)
    pushed = made.push(project, make_package({**BASE, "calc.py": b"x = 3\n"}, state_id(BASE)), "w2")
    with pytest.raises(Refusal) as caught:
        made.record_review("operator", project, pushed.delivery_id, Verdict.ACCEPT, "too soon")
    assert caught.value.reason is Reason.NOT_PENDING
    made.declare(project, pushed.delivery_id)
    while made.step():
        pass
    assert made.status(project, pushed.delivery_id).pull_number is None  # the forge refused
    made.record_review("operator", project, pushed.delivery_id, Verdict.ACCEPT, "fine")
    while made.step():
        pass
    assert forge.comments == {}  # no pull request: nothing was posted
    assert Action.PULL_REQUEST_COMMENT not in [r.action for r in audit.read(project, 0, 1000)]


def test_the_main_agent_hears_of_a_verdict_and_of_a_decision_an_inspection_changes() -> None:
    """ADR 0020: a reject is the main agent's to act on; a new decision is a transition."""
    events: list[tuple[str, Mapping[str, object]]] = []
    made, project, _audit, _store = service(
        events=lambda event, payload: events.append((event, payload))
    )
    with_constraint(made, project)
    ready(made, project)
    delivery = delivered(made, project)
    events.clear()
    made.record_inspection("operator", project, delivery, "CTR-01", Inspected.CONFORMS, "ok")
    made.record_inspection("operator", project, delivery, "CTR-01", Inspected.CONFORMS, "again")
    made.record_review("operator", project, delivery, Verdict.REJECT, "the README is wrong")
    assert events == [
        (
            "delivery.decided",
            {"project_id": project, "delivery_id": delivery, "branch": "work", "decision": "PASS"},
        ),
        (
            "delivery.reviewed",
            {
                "project_id": project,
                "delivery_id": delivery,
                "branch": "work",
                "decision": "PASS",
                "verdict": "reject",
                "reasons": "the README is wrong",
            },
        ),
    ]


def test_the_report_names_the_version_of_each_acceptance_test_that_ran() -> None:
    made, project, _audit, _store = service()
    ready(made, project)
    digest = made.put_acceptance(project, "checks/add.txt", "test_add: pass\n")
    report = made.status(project, delivered(made, project)).report
    assert report is not None
    assert f"`checks/add.txt` (`{digest}`)" in report


def test_the_report_names_the_tests_the_kit_never_ran_and_marks_no_file() -> None:
    """A2a: the third real run's Owner found a test of a named file that never ran, and kept a
    support module its tests used. The report names the test, and of the files only which one
    no criterion names."""
    made, project, audit, _store = service(runner=DeselectingRunner())
    ready(made, project)
    made.put_acceptance(project, "checks/add.txt", "test_add: pass\ntest_add_twice: pass\n")
    made.put_acceptance(project, "checks/support.txt", "helper: used\n")
    record = made.status(project, delivered(made, project))
    assert record.outcomes == {"checks/add.txt::test_add": "passed"}
    assert record.report is not None
    lines = record.report.splitlines()
    assert "- `checks/add.txt::test_add_twice`" in lines
    delivery_line = next(line for line in lines if line.startswith("- Files of the Owner side"))
    assert "`checks/support.txt` (`" in delivery_line
    assert delivery_line.endswith(", named by no criterion)")  # support.txt only
    assert delivery_line.count("named by no criterion") == 1
    runs = [r for r in audit.read(project, 0, 1000) if r.action is Action.VERIFICATION_RUN]
    assert json.loads(runs[-1].parameters)["never_run"] == 1


def test_a_delivery_whose_kit_ran_every_test_of_its_files_has_no_flag() -> None:
    """The computation, not only the rendering: nothing unnamed, nothing deselected, no flag."""
    made, project, _audit, _store = service(runner=DeselectingRunner())
    ready(made, project)
    made.put_acceptance(project, "checks/add.txt", "test_add: pass\n")
    report = made.status(project, delivered(made, project)).report
    assert report is not None
    assert "## Flags\n\nNone." in report
    assert "named by no criterion" not in report


def test_only_tests_of_the_files_the_kit_ran_and_never_one_asked_count_as_never_run() -> None:
    """What a kit names as deselected is taken only where it can be true."""
    result = VerificationResult(
        {"a.py::test_a": VerificationOutcome.PASSED},
        deselected=("a.py::test_b", "a.py::test_a", "elsewhere.py::test_c", "a.py::test_b"),
    )
    assert deselected_tests(result, ("a.py::test_a",)) == ("a.py::test_b",)
    plain = VerificationResult({"a.py::test_a": VerificationOutcome.PASSED})
    assert deselected_tests(plain, ("a.py::test_a",)) == ()  # a kit that ran every test


def _title_of(forge: InMemoryForge, number: int) -> str:
    return asyncio.run(forge.get_pull_request(RepositoryRef("example/calc"), number)).title


def test_an_inspection_that_changes_the_decision_retitles_the_pull_request() -> None:
    """ADR 0020: the third real run's pull request said CONCERNS while the decision was PASS."""
    forge = calc_forge()
    made, project, audit, _store = service(forge=forge)
    with_constraint(made, project)
    ready(made, project)
    delivery = delivered(made, project)
    assert _title_of(forge, 1) == f"work (delivery {delivery}, CONCERNS)"
    made.record_inspection(
        "operator", project, delivery, "CTR-01", Inspected.CONFORMS, "stdlib only"
    )
    assert made.status(project, delivery).decision == "PASS"
    assert _title_of(forge, 1) == f"work (delivery {delivery}, CONCERNS)"  # the worker retitles
    while made.step():
        pass
    assert _title_of(forge, 1) == f"work (delivery {delivery}, PASS)"
    retitles = [r for r in audit.read(project, 0, 1000) if r.action is Action.PULL_REQUEST_RETITLE]
    assert [r.outcome for r in retitles] == [Outcome.OK]


class RetitleRefusingForge(InMemoryForge):
    async def update_title(self, repository: RepositoryRef, number: int, title: str) -> None:
        del repository, number, title
        raise ForgeError("titles are locked")


def test_a_retitle_the_forge_refuses_is_logged_and_the_decision_stays() -> None:
    forge = RetitleRefusingForge(
        {"example/calc": frozenset({"main"})}, {("example/calc", "main"): make_package(BASE)}
    )
    made, project, audit, _store = service(forge=forge)
    with_constraint(made, project)
    ready(made, project)
    delivery = delivered(made, project)
    made.record_inspection(
        "operator", project, delivery, "CTR-01", Inspected.CONFORMS, "stdlib only"
    )
    while made.step():
        pass
    retitles = [r for r in audit.read(project, 0, 1000) if r.action is Action.PULL_REQUEST_RETITLE]
    assert [r.outcome for r in retitles] == [Outcome.ERROR]
    assert "titles are locked" in retitles[0].parameters
    assert made.status(project, delivery).decision == "PASS"


def test_an_inspection_that_does_not_change_the_decision_does_not_retitle() -> None:
    forge = calc_forge()
    made, project, audit, _store = service(forge=forge)
    with_constraint(made, project)
    ready(made, project)
    delivery = delivered(made, project)
    made.record_inspection(
        "operator", project, delivery, "CTR-01", Inspected.CONFORMS, "stdlib only"
    )
    while made.step():
        pass
    retitled = _title_of(forge, 1)
    assert retitled == f"work (delivery {delivery}, PASS)"
    # a second inspection that leaves the decision at PASS changes no title
    made.record_inspection(
        "operator", project, delivery, "CTR-01", Inspected.CONFORMS, "still stdlib"
    )
    while made.step():
        pass
    assert _title_of(forge, 1) == retitled
    retitles = [r for r in audit.read(project, 0, 1000) if r.action is Action.PULL_REQUEST_RETITLE]
    assert len(retitles) == 1


# ----------------------------------------------------------------------------- T02, ATK-16
def checks_logged(audit: AuditLog, project: str, outcome: Outcome) -> list[dict[str, Any]]:
    return [
        json.loads(r.parameters)
        for r in audit.read(project, 0, 1000)
        if r.action is Action.DELIVERY_CHECK and r.outcome is outcome
    ]


def test_a_named_test_file_nobody_holds_does_not_run_and_the_report_tells_the_owner() -> None:
    """T02: a criterion names checks/sub.txt, the Owner side never put it and the delivery does
    not bring it: its test cannot run, and the report says which file is missing."""
    made, project, audit, _store = service(specs=ADD_AND_SUB)
    ready(made, project)
    record = made.status(project, delivered(made, project))
    assert record.outcomes == {
        "checks/add.txt::test_add": "passed",
        "checks/sub.txt::test_sub": "not_run",
    }
    assert record.decision == "FAIL"
    assert record.report is not None
    assert "- `checks/sub.txt`" in record.report.splitlines()  # not set aside: never delivered
    [check] = checks_logged(audit, project, Outcome.OK)
    assert (check["missing"], check["set_aside"], check["flagged"]) == (["checks/sub.txt"], [], 0)


def test_a_named_test_file_the_base_holds_runs_in_the_version_of_the_base() -> None:
    """The delivery leaves checks/add.txt alone; the tree verified holds the base's copy, and
    nothing is flagged or logged as missing."""
    made, project, audit, _store = service()
    ready(made, project)
    record = made.status(project, delivered(made, project))
    assert record.outcomes == {"checks/add.txt::test_add": "passed"}
    assert checks_logged(audit, project, Outcome.OK) == []


def test_a_refusal_names_twenty_paths_at_most_and_counts_them_all() -> None:
    """ATK-16: the refusal names the paths the delivery touched. A record of the log holds 4 KiB
    of parameters: thirty long paths would turn it into a bare "truncated" mark."""
    made, project, audit, _store = service()
    ready(made, project)
    paths = [f"checks/{'x' * 130}{n:02d}.txt" for n in range(30)]
    for path in paths:
        made.put_acceptance(project, path, "test_x: pass\n")
    delivered(made, project, {**BASE, **dict.fromkeys(paths, b"test_x: pass\n")})
    [refusal] = checks_logged(audit, project, Outcome.REFUSED)
    assert (refusal["paths"], refusal["count"]) == (sorted(paths)[:20], 30)


# ----------------------------------------------------------------------------- T05
class BusyForge(InMemoryForge):
    """A forge that takes its time to open a pull request: `meanwhile` runs while it does, as a
    request that a thread of the hub serves while the worker waits on the network."""

    meanwhile: Callable[[], object] = staticmethod(lambda: None)

    async def open_pull_request(self, request: PullRequestRequest) -> PullRequest:
        self.meanwhile()
        return await super().open_pull_request(request)


def busy_forge() -> BusyForge:
    return BusyForge(
        {"example/calc": frozenset({"main"})}, {("example/calc", "main"): make_package(BASE)}
    )


def test_an_inspection_recorded_while_the_pull_request_opens_is_kept_posted_and_retitled() -> None:
    """T05: the worker wrote the decision, then waited on the forge. An inspection recorded
    meanwhile must survive the worker's next write, and reach the pull request once it exists:
    its comment, and the title of the decision it leads to."""
    forge = busy_forge()
    told: list[Mapping[str, object]] = []
    made, project, _audit, _store = service(
        forge=forge,
        events=lambda event, payload: told.append(payload) if event == "delivery.decided" else None,
    )
    with_constraint(made, project)
    ready(made, project)
    forge.meanwhile = lambda: made.record_inspection(
        "operator", project, "d1", "CTR-01", Inspected.CONFORMS, "stdlib only"
    )
    delivery = delivered(made, project)
    assert told[-1]["decision"] == "PASS"  # the last decision the main agent hears of is in force
    record = made.status(project, delivery)
    assert record.inspections["CTR-01"]["verdict"] == "conforms"
    assert (record.state, record.decision, record.pull_number) == (
        DeliveryState.PROPOSED,
        "PASS",
        1,
    )
    [comment] = forge.comments[("example/calc", 1)]
    assert "**Inspection recorded** by operator: CTR-01 conforms." in comment
    assert _title_of(forge, 1) == f"work (delivery {delivery}, PASS)"


class RacingStore(InMemoryDeliveryStore):
    """A store where, once, another writer gets in between a read and the write that follows."""

    cut_in: Callable[[DeliveryRecord], DeliveryRecord] | None = None

    def put_record(self, record: DeliveryRecord) -> DeliveryRecord:
        cut_in, self.cut_in = self.cut_in, None
        if cut_in is not None:
            stored = self.get_record(record.project_id, record.delivery_id)
            assert stored is not None
            super().put_record(cut_in(stored))
        return super().put_record(record)


def test_a_write_that_lost_the_race_is_made_again_on_the_record_as_it_is_now() -> None:
    """T05: a verdict lands between the read and the write of an inspection. The store refuses
    the stale write, and the inspection is applied again: both stay."""
    store = RacingStore()
    made, project, _audit, _store = service(store=store)
    with_constraint(made, project)
    ready(made, project)
    delivery = delivered(made, project)
    verdict = {"verdict": "accept", "reasons": "fine", "by": "operator", "at": "now"}
    store.cut_in = lambda record: replace(record, review=verdict)
    answered = made.record_inspection(
        "operator", project, delivery, "CTR-01", Inspected.CONFORMS, "stdlib only"
    )
    kept = made.status(project, delivery)
    assert kept == answered
    assert (kept.review, kept.inspections["CTR-01"]["verdict"], kept.decision) == (
        verdict,
        "conforms",
        "PASS",
    )


def test_a_push_never_writes_over_a_recorded_delivery() -> None:
    """The area numbers deliveries; an area emptied under a kept database would number a new
    one like an old one. The old record stays, and the push is refused."""
    made, project, _audit, store = service()
    ready(made, project)
    old = store.put_record(
        DeliveryRecord(project, "d1", "old", DeliveryState.DECIDED, "2026-10-01T00:00:00Z")
    )
    with pytest.raises(Refusal, match="already recorded"):
        made.push(project, make_package({**BASE, "calc.py": b"x = 3\n"}, state_id(BASE)), "w")
    assert made.status(project, "d1") == old


# ----------------------------------------------------------------------------- T07
HIDDEN = "assert sub(7, 3) == 4  # the Owner side's own check"


class TracebackRunner(AssertionRunner):
    """The assertions runner, whose output shows the file of every test that did not pass, as
    a pytest traceback shows the lines of the test."""

    def run(self, request: VerificationRequest) -> VerificationResult:
        result = super().run(request)
        shown = [
            request.files[test.partition("::")[0]].decode()
            for test, outcome in result.outcomes.items()
            if outcome is not VerificationOutcome.PASSED
        ]
        return VerificationResult(result.outcomes, "\n".join([result.log, *shown]))


def test_the_contractor_reads_every_outcome_and_never_the_output_of_the_run() -> None:
    """T07: the output of the run shows the Owner side's acceptance tests, which the Contractor
    must not read; it reads the outcome of each test. The Owner side reads it all."""
    made, project, _audit, _store = service(runner=TracebackRunner(), specs=ADD_AND_SUB)
    ready(made, project)
    made.put_acceptance(project, "checks/sub.txt", f"test_sub: fail\n{HIDDEN}\n")
    record = made.status(project, delivered(made, project))
    owner = report_for(record, instance_principal(Side.OWNER, project))
    contractor = report_for(record, instance_principal(Side.CONTRACTOR, project))
    assert owner is not None
    assert contractor is not None
    assert HIDDEN in owner
    assert HIDDEN not in contractor
    for test, outcome in (
        ("checks/add.txt::test_add", "passed"),
        ("checks/sub.txt::test_sub", "failed"),
    ):
        assert f"- `{test}`: {outcome}" in contractor.splitlines()
    assert report_for(record, OPERATOR) == report_for(record, MAIN_AGENT) == owner == record.report


# ----------------------------------------------------------------------------- T03
@pytest.mark.parametrize(
    ("runner", "files", "decision"),
    [
        (None, {**BASE, "checks/add.txt": b"test_add: pass\n# mine\n"}, "REFUSED"),
        (Exploding(), None, "ERROR"),
        (None, None, "PASS"),
        (Failing(), None, "FAIL"),
    ],
)
def test_the_main_agent_hears_once_of_what_became_of_a_delivery(
    runner: AssertionRunner | None, files: dict[str, bytes] | None, decision: str
) -> None:
    """T03: a refusal and an error are told like a decision of the gate, once each."""
    events: list[tuple[str, Mapping[str, object]]] = []
    made, project, _audit, _store = service(
        runner=runner, events=lambda event, payload: events.append((event, payload))
    )
    ready(made, project)
    delivery = delivered(made, project, files)
    told = [payload for event, payload in events if event == "delivery.decided"]
    assert told == [
        {"project_id": project, "delivery_id": delivery, "branch": "work", "decision": decision}
    ]


def test_a_receiver_that_fails_keeps_neither_the_decision_nor_the_pull_request_back() -> None:
    """The worker tells of a decision once the forge has answered: a notification that fails
    leaves the record decided, and its pull request open."""

    def unreachable(event: str, payload: Mapping[str, object]) -> None:
        if event == "delivery.decided":
            raise ConnectionError("the receiver is down")

    forge = calc_forge()
    made, project, _audit, _store = service(forge=forge, events=unreachable)
    ready(made, project)
    pushed = made.push(project, make_package({**BASE, "calc.py": b"x = 2\n"}, state_id(BASE)), "w")
    made.declare(project, pushed.delivery_id)
    with pytest.raises(ConnectionError):
        made.step()  # the worker's loop reports it and goes on
    record = made.status(project, pushed.delivery_id)
    assert (record.state, record.decision, record.pull_number) == (
        DeliveryState.PROPOSED,
        "PASS",
        1,
    )


class RefusalUnlogged(InMemoryLogStore):
    """A log that cannot take the record of a refusal: the job fails once the refusal is kept."""

    def append(self, record: AuditRecord) -> None:
        if record.action is Action.DELIVERY_CHECK and record.outcome is Outcome.REFUSED:
            raise LogError("the log is full")
        super().append(record)


def test_a_failure_after_the_checks_refused_a_delivery_leaves_it_refused_and_told() -> None:
    """The job fails after the refusal is kept: the delivery stays refused, never ERROR, and the
    main agent hears of it once, like any refusal."""
    events: list[tuple[str, Mapping[str, object]]] = []
    made, project, audit, _store = service(
        events=lambda event, payload: events.append((event, payload)),
        log_store=RefusalUnlogged(),
    )
    ready(made, project)
    delivery = delivered(made, project, {**BASE, "checks/add.txt": b"test_add: pass\n# mine\n"})
    record = made.status(project, delivery)
    assert record.state is DeliveryState.REFUSED
    assert audit.read(project, 0, 1000)[-1].action is Action.VERIFICATION_RUN  # the error, logged
    told = [payload["decision"] for event, payload in events if event == "delivery.decided"]
    assert told == ["REFUSED"]


class NothingFailedLogged(RefusalUnlogged):
    """A log that takes neither the record of a refusal nor the record of an error."""

    def append(self, record: AuditRecord) -> None:
        if record.action is Action.VERIFICATION_RUN and record.outcome is Outcome.ERROR:
            raise LogError("the log is full")
        super().append(record)


@pytest.mark.parametrize(
    ("runner", "files", "state"),
    [
        (None, {**BASE, "checks/add.txt": b"test_add: pass\n# mine\n"}, DeliveryState.REFUSED),
        (Exploding(), {**BASE, "calc.py": b"x = 2\n"}, DeliveryState.ERROR),
    ],
)
def test_a_failure_the_log_cannot_take_is_still_told_once(
    runner: AssertionRunner | None, files: dict[str, bytes], state: DeliveryState
) -> None:
    """EXG-23, review of #15: the log refuses the record of the error too. The delivery is told
    once all the same, as a refusal or as an error, and the worker hears of the log's fault."""
    events: list[tuple[str, Mapping[str, object]]] = []
    made, project, audit, _store = service(
        runner=runner,
        events=lambda event, payload: events.append((event, payload)),
        log_store=NothingFailedLogged(),
    )
    ready(made, project)
    pushed = made.push(project, make_package(files, state_id(BASE)), "work")
    made.declare(project, pushed.delivery_id)
    with pytest.raises(LogError):
        made.step()  # the worker's loop reports it and goes on
    assert made.status(project, pushed.delivery_id).state is state
    assert audit.read(project, 0, 1000)[-1].action is not Action.VERIFICATION_RUN
    told = [payload["decision"] for event, payload in events if event == "delivery.decided"]
    assert told == [state.name]


class OpeningUnlogged(InMemoryLogStore):
    """A log that takes no record of a pull request opened, or not."""

    def append(self, record: AuditRecord) -> None:
        if record.action is Action.PULL_REQUEST_OPEN:
            raise LogError("the log is full")
        super().append(record)


class ProposalContended(InMemoryDeliveryStore):
    """A store where another writer always gets in first once the pull request is open."""

    def put_record(self, record: DeliveryRecord) -> DeliveryRecord:
        if record.state is DeliveryState.PROPOSED:
            raise StaleRecordError("written by someone else")
        return super().put_record(record)


@pytest.mark.parametrize(
    ("forge", "log_store", "store", "fault"),
    [
        ("open", OpeningUnlogged(), None, LogError),
        ("refusing", OpeningUnlogged(), None, LogError),
        ("open", None, ProposalContended(), StaleRecordError),
    ],
    ids=["opened-unlogged", "refused-unlogged", "proposal-unwritten"],
)
def test_a_decision_is_told_once_whatever_becomes_of_its_pull_request(
    forge: str,
    log_store: InMemoryLogStore | None,
    store: InMemoryDeliveryStore | None,
    fault: type[Exception],
) -> None:
    """EXG-23, review of #15: a PASS whose pull request the log cannot record, opened or
    refused, or whose proposal is never written. The main agent hears of the decision once,
    and the worker hears of the fault."""
    events: list[tuple[str, Mapping[str, object]]] = []
    made, project, _audit, _store = service(
        forge=calc_forge()
        if forge == "open"
        else RefusingForge(
            {"example/calc": frozenset({"main"})}, {("example/calc", "main"): make_package(BASE)}
        ),
        log_store=log_store,
        store=store,
        events=lambda event, payload: events.append((event, payload)),
    )
    ready(made, project)
    pushed = made.push(project, make_package({**BASE, "calc.py": b"x = 2\n"}, state_id(BASE)), "w")
    made.declare(project, pushed.delivery_id)
    with pytest.raises(fault):
        made.step()  # the worker's loop reports it and goes on
    assert made.status(project, pushed.delivery_id).decision == "PASS"
    told = [payload["decision"] for event, payload in events if event == "delivery.decided"]
    assert told == ["PASS"]


class Contended(InMemoryDeliveryStore):
    """A store where, once `busy`, another writer always gets in first."""

    busy = False
    attempts = 0

    def put_record(self, record: DeliveryRecord) -> DeliveryRecord:
        if self.busy:
            self.attempts += 1
            raise StaleRecordError("written by someone else")
        return super().put_record(record)


def test_a_change_that_is_never_written_fails_after_a_bounded_number_of_attempts() -> None:
    store = Contended()
    made, project, _audit, _store = service(store=store)
    ready(made, project)
    delivery = delivered(made, project)
    store.busy = True
    with pytest.raises(StaleRecordError):
        made.record_review("operator", project, delivery, Verdict.ACCEPT, "fine")
    assert store.attempts == WRITE_ATTEMPTS
    assert made.status(project, delivery).review is None


class TitleRecordingForge(InMemoryForge):
    """Keeps every title the worker sets, in order."""

    titles: list[str]

    async def update_title(self, repository: RepositoryRef, number: int, title: str) -> None:
        self.titles.append(title)
        await super().update_title(repository, number, title)


def test_the_worker_titles_the_pull_request_with_the_decision_when_the_job_runs() -> None:
    """C50: two inspections change the decision before the worker runs; each retitle job sets
    the title of the decision in force when it runs, never the one of when it was queued."""
    forge = TitleRecordingForge(
        {"example/calc": frozenset({"main"})}, {("example/calc", "main"): make_package(BASE)}
    )
    forge.titles = []
    made, project, _audit, _store = service(forge=forge)
    with_constraint(made, project)
    ready(made, project)
    delivery = delivered(made, project)
    made.record_inspection("operator", project, delivery, "CTR-01", Inspected.CONFORMS, "ok")
    made.record_inspection(
        "operator", project, delivery, "CTR-01", Inspected.DOES_NOT_CONFORM, "imports yaml"
    )
    assert forge.titles == []  # the worker retitles, never the request
    while made.step():
        pass
    assert forge.titles == [f"work (delivery {delivery}, FAIL)"] * 2


# ----------------------------------------------------------------------------- ATK-84, EXG-49
def test_a_branch_name_that_holds_a_secret_is_refused_before_the_area_keeps_it() -> None:
    made, project, _audit, store = service(secrets=lambda: ["s3cretvalue"])
    ready(made, project)
    package = make_package({**BASE, "calc.py": b"x = 2\n"}, state_id(BASE))
    with pytest.raises(Refusal, match=r"the branch name work-\[masked\] holds a secret") as caught:
        made.push(project, package, "work-s3cretvalue")
    assert caught.value.reason is Reason.INVALID_REQUEST
    assert "s3cretvalue" not in str(caught.value)
    assert store.records(project) == []
    assert made.push(project, package, "work").delivery_id == "d1"  # the area kept nothing


def test_a_secret_the_hub_learns_after_the_push_stays_off_the_forge_and_the_events() -> None:
    """A record kept before the name of its branch was checked, or a secret the hub learned
    since: the title is masked, the branch pushed to the forge leaves the name out, and the
    events mask it."""
    known: list[str] = []
    told: list[Mapping[str, object]] = []
    forge = calc_forge()
    made, project, _audit, _store = service(
        forge=forge,
        secrets=lambda: list(known),
        events=lambda event, payload: told.append(payload) if event == "delivery.decided" else None,
    )
    ready(made, project)
    pushed = made.push(
        project, make_package({**BASE, "calc.py": b"x = 2\n"}, state_id(BASE)), "work-s3cretvalue"
    )
    known.append("s3cretvalue")
    made.declare(project, pushed.delivery_id)
    while made.step():
        pass
    opened = asyncio.run(forge.get_pull_request(RepositoryRef("example/calc"), 1))
    assert (opened.title, opened.head) == ("work-[masked] (delivery d1, PASS)", "zephyros/d1")
    assert [payload["branch"] for payload in told] == ["work-[masked]"]


def test_a_decision_that_cannot_be_told_still_raises_the_alert_of_a_loop() -> None:
    """EXG-23: telling a FAIL fails (a receiver that is down); the streak of failed deliveries
    still reaches the main agent, and the worker hears of the failure."""
    loops: list[Mapping[str, object]] = []

    def receiver(event: str, payload: Mapping[str, object]) -> None:
        if event == "delivery.decided":
            raise ConnectionError("the receiver is down")
        if event == "delivery.loop":
            loops.append(payload)

    made, project, _audit, _store = service(runner=Failing(), events=receiver, rejections_alert=1)
    ready(made, project)
    pushed = made.push(project, make_package({**BASE, "calc.py": b"x = 2\n"}, state_id(BASE)), "w")
    made.declare(project, pushed.delivery_id)
    with pytest.raises(ConnectionError):
        made.step()
    assert made.status(project, pushed.delivery_id).decision == "FAIL"
    assert loops == [{"project_id": project, "branch": "w", "rejections": 1}]


# ----------------------------------------------------------------------------- review of #9
def test_a_record_without_a_gate_report_keeps_one_addendum_however_often_it_is_inspected() -> None:
    """A record built by hand, as the replay tools do, holds its report and no report of the
    gate: the first inspection takes that report as the report of the gate, and the next ones
    write the addendum whole after it, never one more."""
    made, project, _audit, store = service()
    with_constraint(made, project)
    ready(made, project)
    delivery = delivered(made, project)
    decided = made.status(project, delivery)
    store.put_record(replace(decided, gate_report=None))
    for verdict in (Inspected.CONFORMS, Inspected.DOES_NOT_CONFORM, Inspected.CONFORMS):
        made.record_inspection("operator", project, delivery, "CTR-01", verdict, "looked")
    record = made.status(project, delivery)
    assert record.report is not None
    assert record.report.count("## Inspections recorded after the gate") == 1
    assert record.gate_report == decided.report


def test_a_record_kept_before_the_contractors_report_gives_the_contractor_none() -> None:
    """A record of a hub before the Contractor's report: the Contractor reads no report, the
    Owner side reads the whole one."""
    made, project, _audit, store = service()
    ready(made, project)
    delivery = delivered(made, project)
    decided = made.status(project, delivery)
    store.put_record(replace(decided, contractor_gate_report=None))
    kept = made.status(project, delivery)
    assert report_for(kept, instance_principal(Side.CONTRACTOR, project)) is None
    assert report_for(kept, instance_principal(Side.OWNER, project)) == decided.report
    assert decided.report is not None


# ----------------------------------------------------------------------------- context (ADR 0033)
def report_after_turns(*contexts: tuple[int, int, int], simulation: bool = False) -> str:
    """The report of a delivery pushed after turns that ran with these (context, window,
    compaction point)."""
    made, project, audit, _store = service(simulation=simulation)
    ready(made, project)
    for side in ("owner", "contractor"):
        for tokens, window, compaction in contexts:
            audit.append(
                AuditEvent(
                    project,
                    "hub",
                    Action.TURN_START,
                    Outcome.OK,
                    parameters={
                        "side": side,
                        "context_tokens": tokens,
                        "context_window": window,
                        "compaction_tokens": compaction,
                    },
                )
            )
    record = made.push(project, make_package({**BASE, "calc.py": b"x = 1\n"}, state_id(BASE)), "w")
    made.declare(project, record.delivery_id)
    while made.step():
        pass
    report = made.status(project, record.delivery_id).report
    assert report is not None
    return report


def test_the_report_gives_the_context_of_the_turns_before_the_delivery() -> None:
    report = report_after_turns((1_000_000, 1_000_000, 900_000))
    assert (
        "- Context of the agents: 1000000 tokens of a window of 1000000, compacting at 900000 "
        "(2 turns)"
    ) in report.splitlines()
    assert "CONTEXT BELOW THE FLOOR" not in report


def test_a_delivery_built_below_the_floor_opens_its_report_with_a_warning() -> None:
    """The floor is about the compaction point: the default window compacts at 224 000."""
    report = report_after_turns((80_000, 1_000_000, 80_000), (256_000, 256_000, 224_000))
    [warning] = [line for line in report.splitlines() if "CONTEXT BELOW THE FLOOR" in line]
    assert report.index(warning) < report.index("**Decision:")
    assert "4 turns before this delivery compacted at 80000 tokens or less" in warning
    assert "a long task loses the thread of its work" in warning
    assert "2026" not in warning


def test_a_simulation_has_no_context_to_lose() -> None:
    report = report_after_turns((256_000, 256_000, 224_000), simulation=True)
    assert "CONTEXT BELOW THE FLOOR" not in report
    assert "Context of the agents" in report


def test_a_report_of_turns_that_recorded_no_context_says_nothing_of_it() -> None:
    report = report_after_turns()
    assert "Context of the agents" not in report
    assert "CONTEXT BELOW THE FLOOR" not in report
