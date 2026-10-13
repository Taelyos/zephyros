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
from zephyros.core.audit import Action, AuditEvent, AuditLog, Outcome, Reason
from zephyros.core.contract import ContractService, parse_change
from zephyros.core.deliveries import (
    DeliveryRecord,
    DeliveryService,
    DeliveryState,
    Verdict,
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

BASE = {"calc.py": b"x = 1\n", "checks/add.txt": b"test_add: pass\n"}


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
    events: list[tuple[str, Mapping[str, object]]] | None = None,
) -> tuple[DeliveryService, str, AuditLog, InMemoryDeliveryStore]:
    """A delivery service on a project with a contract; `events`, when given, collects what
    the service tells the hub."""
    clock = FakeClock()
    audit = AuditLog(InMemoryLogStore(), clock)
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
        events=None if events is None else lambda event, payload: events.append((event, payload)),
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


def test_a_delivery_declared_before_the_contract_runs_nothing_and_never_passes() -> None:
    """T10: it passed for want of anything to judge. The runner would fail if it ran."""
    made, project, _audit, _store = service(runner=Exploding(), specs=())
    ready(made, project)
    record = made.status(project, delivered(made, project))
    assert (record.outcomes, record.decision, record.contract_version) == ({}, "CONCERNS", 0)
    assert record.report is not None
    assert f"- {NOTHING_JUDGED}" in record.report.splitlines()


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
    made, project, _audit, _store = service(events=events)
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


def test_an_acceptance_file_no_criterion_names_is_flagged_in_the_report() -> None:
    """C49: the Owner of the third real run put a test no criterion named; it never ran."""
    made, project, _audit, _store = service()
    ready(made, project)
    made.put_acceptance(project, "checks/extra.txt", "test_extra: pass\n")
    report = made.status(project, delivered(made, project)).report
    assert report is not None
    assert "Acceptance test files that no criterion names (they never run):" in report
    assert "- `checks/extra.txt`" in report


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
    made, project, _audit, _store = service(forge=forge)
    with_constraint(made, project)
    ready(made, project)
    forge.meanwhile = lambda: made.record_inspection(
        "operator", project, "d1", "CTR-01", Inspected.CONFORMS, "stdlib only"
    )
    delivery = delivered(made, project)
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
    made, project, _audit, _store = service(runner=runner, events=events)
    ready(made, project)
    delivery = delivered(made, project, files)
    told = [payload for event, payload in events if event == "delivery.decided"]
    assert told == [
        {"project_id": project, "delivery_id": delivery, "branch": "work", "decision": decision}
    ]
