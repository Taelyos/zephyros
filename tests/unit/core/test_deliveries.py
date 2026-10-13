"""The delivery service when something goes wrong: no forge, a forge or a runner that fails."""

from __future__ import annotations

from collections.abc import Mapping
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
from zephyros.core.deliveries import DeliveryService, DeliveryState, Verdict, flagged_paths
from zephyros.core.errors import Refusal
from zephyros.core.gate import Inspected
from zephyros.core.ports.forge import ForgeError, RepositoryRef
from zephyros.core.ports.verification import Outcome as VerificationOutcome
from zephyros.core.ports.verification import (
    VerificationError,
    VerificationRequest,
    VerificationResult,
)
from zephyros.core.principals import OPERATOR
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


def service(
    runner: Any = None, forge: Any = "default"
) -> tuple[DeliveryService, str, AuditLog, InMemoryDeliveryStore]:
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
    if forge == "default":
        forge = InMemoryForge(
            {"example/calc": frozenset({"main"})},
            {("example/calc", "main"): make_package(BASE)},
        )
    store = InMemoryDeliveryStore()
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


def test_a_contract_without_acceptance_tests_runs_nothing_and_passes_vacuously() -> None:
    made, project, _audit, _store = service()
    made._contracts = ContractService(InMemoryContractStore(), FakeClock())  # an empty contract
    ready(made, project)
    record = made.status(project, delivered(made, project))
    assert record.outcomes == {}
    assert record.decision == "PASS"


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
    made, project, _audit, _store = service()
    ready(made, project)
    pushed = made.push(project, make_package({**BASE, "calc.py": b"x = 3\n"}, state_id(BASE)), "w2")
    with pytest.raises(Refusal) as caught:
        made.record_review("operator", project, pushed.delivery_id, Verdict.ACCEPT, "too soon")
    assert caught.value.reason is Reason.NOT_PENDING
    made.declare(project, pushed.delivery_id)
    while made.step():
        pass
    decided = replace(made.status(project, pushed.delivery_id), pull_number=None)
    made._store.put_record(decided)
    made.record_review("operator", project, pushed.delivery_id, Verdict.ACCEPT, "fine")
    assert not made.step()  # no pull request: nothing to post


def test_the_main_agent_hears_of_a_verdict_and_of_a_decision_an_inspection_changes() -> None:
    """ADR 0020: a reject is the main agent's to act on; a new decision is a transition."""
    events: list[tuple[str, Mapping[str, object]]] = []
    made, project, _audit, _store = service()
    made._events = lambda event, payload: events.append((event, payload))
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
