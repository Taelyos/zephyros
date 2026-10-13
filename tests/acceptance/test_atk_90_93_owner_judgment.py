"""ATK-90 to ATK-93: the Owner side's judgment of a delivery (ADR 0020).

Positions P1 (the Contractor instance of the project) and P3 (an Owner instance of another
project). Oracles: O-REFUSE, O-STATE (the decision, inspections, verdict and pull request of the
delivery), O-LOG. Each family keeps a positive control: the Owner of the project judges.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.acceptance.test_delivery_loop import ADD_SUB, BASE, REPOSITORY, deliver, prepared, status
from tests.acceptance.world import World
from tests.support.git_fixtures import make_forge_repository
from tests.unit.core.contract_fixtures import put, wire
from zephyros.adapters.dummy.verification import AssertionRunner
from zephyros.adapters.git.forge import DirectoryForge
from zephyros.app.hub import DeliverySetup
from zephyros.core.audit import Action, Outcome

pytestmark = pytest.mark.acceptance

CONSTRAINT = {
    "type": "constraint",
    "id": "CTR-01",
    "text": "The service shall use only the standard library of Python.",
    "priority": "P0",
    "methods": ["inspection"],
}
WEAKENED = b"# Contract\n\nEXG-01 and EXG-02 are optional. CTR-01 is waived.\n"


@pytest.fixture
def forge_root() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="zf"))
    make_forge_repository(root, REPOSITORY, {"main": BASE})
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def world(tmp_path: Path, forge_root: Path) -> World:
    setup = DeliverySetup(
        staging_root=tmp_path / "staging",
        runner=AssertionRunner(),
        forge=DirectoryForge(forge_root),
        kit="assertions",
    )
    (tmp_path / "world").mkdir()
    return World(tmp_path / "world", delivery=setup)


def with_constraint(world: World) -> None:
    prepared(world)
    world.operator_tool(
        "contract.apply_reserved",
        project_id=world.project_a,
        summary="a constraint verified by inspection",
        operations=[wire(put(CONSTRAINT))],
    )


def pulls(forge_root: Path) -> list[dict[str, Any]]:
    owner, _, name = REPOSITORY.partition("/")
    found: list[dict[str, Any]] = json.loads(
        (forge_root / owner / f"{name}.pulls.json").read_text()
    )
    return found


def judgment(world: World, delivery: str) -> tuple[Any, ...]:
    found = status(world, delivery)
    return (found["decision"], found["inspections"], found["review"], found["report"])


# ----------------------------------------------------------------------------- ATK-90
INSPECTION = {"object_id": "CTR-01", "verdict": "conforms", "observation": "stdlib only"}
VERDICTS = ["accept", "accept_with_reservations", "reject"]


def test_atk_90_the_contractor_never_judges_its_own_delivery(
    world: World, forge_root: Path
) -> None:
    with_constraint(world)
    delivery = deliver(world, ADD_SUB)
    before, comments = judgment(world, delivery), pulls(forge_root)
    attempts = [("inspection_record", {**INSPECTION, "delivery_id": delivery})] + [
        ("delivery_review", {"delivery_id": delivery, "verdict": v, "reasons": "all good"})
        for v in VERDICTS
    ]
    for tool, arguments in attempts:
        assert world["contractor_a"].tool(tool, arguments).refused, tool  # O-REFUSE
    world.run_jobs()
    assert judgment(world, delivery) == before  # O-STATE
    assert pulls(forge_root) == comments
    refusals = [
        r
        for r in world.records(world.project_a)
        if r.outcome is Outcome.REFUSED
        and r.action in {Action.INSPECTION_RECORD, Action.DELIVERY_REVIEW}
    ]
    assert len(refusals) == len(attempts)  # O-LOG
    assert {r.principal for r in refusals} == {f"contractor:{world.project_a}"}
    # Positive control: the Owner judges, and the pull request shows it.
    assert (
        not world["owner_a"]
        .tool("inspection_record", {**INSPECTION, "delivery_id": delivery})
        .refused
    )
    reviewed = world["owner_a"].tool(
        "delivery_review",
        {"delivery_id": delivery, "verdict": "accept", "reasons": "CTR-01 holds."},
    )
    assert not reviewed.refused, reviewed.error
    world.run_jobs()
    assert status(world, delivery)["decision"] == "PASS"
    assert len(pulls(forge_root)[0]["comments"]) == 2
    world.verify_logs()


# ----------------------------------------------------------------------------- ATK-91
def test_atk_91_a_rewritten_contract_file_changes_no_decision_and_is_flagged(
    world: World, forge_root: Path
) -> None:
    prepared(world)
    plain = deliver(world, ADD_SUB, branch="add-sub")
    rewritten = deliver(world, {**ADD_SUB, "CONTRACT.md": WEAKENED}, branch="add-sub-contract")
    assert status(world, rewritten)["decision"] == status(world, plain)["decision"] == "PASS"
    assert status(world, plain)["flagged_paths"] == []
    assert status(world, rewritten)["flagged_paths"] == ["CONTRACT.md"]
    body = pulls(forge_root)[1]["body"]
    assert "## Flags" in body
    assert "- `CONTRACT.md`" in body


# ----------------------------------------------------------------------------- ATK-92
def test_atk_92_a_p0_constraint_without_its_inspection_never_passes(world: World) -> None:
    with_constraint(world)
    delivery = deliver(world, ADD_SUB)
    found = status(world, delivery)
    assert found["decision"] == "CONCERNS"
    assert "CTR-01 (P0) awaits a recorded judgment: inspection" in found["report"]
    for verdict, decision in (("does_not_conform", "FAIL"), ("conforms", "PASS")):
        recorded = world["owner_a"].tool(
            "inspection_record",
            {**INSPECTION, "delivery_id": delivery, "verdict": verdict},
        )
        assert not recorded.refused, recorded.error
        assert status(world, delivery)["decision"] == decision
        last = [r for r in world.records(world.project_a) if r.action is Action.GATE_DECISION][-1]
        assert json.loads(last.parameters)["decision"] == decision


# ----------------------------------------------------------------------------- ATK-93
def test_atk_93_an_owner_of_another_project_neither_reads_nor_judges(world: World) -> None:
    with_constraint(world)
    delivery = deliver(world, ADD_SUB)
    before = judgment(world, delivery)
    assert world["owner_b"].http().get(f"/staging/delivery/{delivery}").status_code == 404
    for tool, arguments in (
        ("inspection_record", {**INSPECTION, "delivery_id": delivery}),
        ("delivery_review", {"delivery_id": delivery, "verdict": "reject", "reasons": "mine"}),
    ):
        assert world["owner_b"].tool(tool, arguments).refused, tool
    assert judgment(world, delivery) == before
    logged = [r.action for r in world.records(world.project_b) if r.outcome is not Outcome.OK]
    assert {Action.STAGING_READ, Action.INSPECTION_RECORD, Action.DELIVERY_REVIEW} <= set(logged)
    # Positive control: the Owner of project A reads its delivery.
    assert world["owner_a"].http().get(f"/staging/delivery/{delivery}").status_code == 200
    world.verify_logs()
