"""ATK-88 and ATK-89, and the criteria of US-E2-02: proposal, approval, rejection, and the floor
of operator-reserved data.

Positions P2 (an Owner instance) and P4 (the main agent), neither of them the operator.
Oracles: O-REFUSE, O-STATE, O-LOG. The operator channel is unreachable from every execution
context (ATK-13), so only it changes reserved data.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.acceptance.world import World
from tests.unit.core.contract_fixtures import (
    constraint,
    criterion,
    delete,
    link,
    note,
    put,
    requirement,
    story,
    wire,
)
from zephyros.core.audit import Action, Outcome, Reason
from zephyros.core.contract import reserved_view

pytestmark = pytest.mark.acceptance

ATTEMPTS: dict[str, list[Any]] = {
    "change the text of a requirement": [put(requirement(1, "Weakened.", "P0"))],
    "change the priority of a requirement": [put(requirement(2, priority="P0"))],
    "change the priority of a P0 requirement": [put(requirement(1, priority="P1"))],
    "change the verification method": [put(requirement(2, methods=("analysis",)))],
    "change the text of a constraint": [put({**constraint(1, "P0"), "text": "Changed."})],
    "add a requirement": [put(requirement(3))],
    "remove a requirement": [delete("LNK:US-E1-02>EXG-02"), delete("EXG-02")],
    "remove the link between a story and a P0 requirement": [delete("LNK:US-E1-01>EXG-01")],
    "add a link to a P0 requirement": [put(link(1, 2, "EXG-01"))],
    "link a new story to a P0 requirement": [put(story(1, 3)), put(link(1, 3, "EXG-01"))],
    "edit a criterion of a P0 story": [put(criterion(1, 1, 1, "Weakened."))],
    "unlink then edit in one change": [
        delete("LNK:US-E1-01>EXG-01"),
        put(criterion(1, 1, 1, "Weakened.")),
    ],
    "delete a P0 criterion": [delete("US-E1-01.1")],
    "add a P0 criterion": [put(criterion(1, 1, 2))],
}


@pytest.mark.parametrize("attempt", sorted(ATTEMPTS))
@pytest.mark.parametrize("name", ["owner_a", "main"])
def test_atk_88_the_owner_side_cannot_change_what_belongs_to_the_operator(
    restarted: World, name: str, attempt: str
) -> None:
    world = restarted
    before = world.fingerprint(world.project_a)
    view_before = reserved_view(_objects(world))
    world.step(f"ATK-88.{attempt}")
    result = world[name].tool(
        "contract_propose",
        {
            "project_id": world.project_a,
            "summary": attempt,
            "operations": [wire(o) for o in ATTEMPTS[attempt]],
        },
    )
    assert result.refused
    assert result.error is not None
    assert result.error["reason"] == Reason.RESERVED_DATA.value
    assert world.fingerprint(world.project_a) == before
    last = world.records(world.project_a)[-1]
    assert (last.action, last.outcome, last.reason) == (
        Action.CONTRACT_PROPOSE,
        Outcome.REFUSED,
        Reason.RESERVED_DATA,
    )
    assert reserved_view(_objects(world)) == view_before
    world.verify_logs()


def _objects(world: World) -> dict[str, dict[str, Any]]:
    from zephyros.core.principals import OPERATOR

    version = world.hub._contracts.read(OPERATOR, world.project_a)  # reason: oracle
    return {k: dict(v) for k, v in version.objects.items()}


def test_atk_88_only_the_operator_makes_these_changes(restarted: World) -> None:
    world = restarted
    result = world.operator_tool(
        "contract.apply_reserved",
        project_id=world.project_a,
        summary="amendment",
        operations=[wire(o) for o in ATTEMPTS["change the text of a requirement"]],
    )
    assert result["number"] == 2
    log = world.records(world.project_a)[-1]
    assert (log.principal, log.action) == ("operator", Action.CONTRACT_APPLY_RESERVED)


def test_atk_88_what_the_owner_side_may_change_it_changes_through_approval(
    restarted: World,
) -> None:
    """Positive control: a plain note, proposed by an Owner and approved by the main agent."""
    world = restarted
    proposal = world["owner_a"].tool(
        "contract_propose",
        {
            "project_id": world.project_a,
            "summary": "a note",
            "operations": [wire(put(note(2, "EXG-02")))],
        },
    )
    assert proposal.data is not None
    approved = world["main"].tool(
        "contract_approve", {"project_id": world.project_a, "change_id": proposal.data["change_id"]}
    )
    assert approved.data is not None
    assert approved.data["number"] == 2


# ----------------------------------------------------------------------------- ATK-89
def propose_note(world: World, name: str, number: int = 2) -> str:
    result = world[name].tool(
        "contract_propose",
        {
            "project_id": world.project_a,
            "summary": "a note",
            "operations": [wire(put(note(number, "EXG-02")))],
        },
    )
    assert result.data is not None
    return str(result.data["change_id"])


def approve(world: World, name: str, change_id: str) -> Any:
    return world[name].tool(
        "contract_approve", {"project_id": world.project_a, "change_id": change_id}
    )


def test_atk_89_the_proposer_cannot_approve_its_own_proposal(restarted: World) -> None:
    world = restarted
    change_id = propose_note(world, "owner_a")
    before = world.fingerprint(world.project_a)
    world.step("ATK-89.same token")
    result = approve(world, "owner_a", change_id)
    assert result.refused
    assert result.error is not None
    assert result.error["reason"] == Reason.SELF_APPROVAL.value
    assert world.fingerprint(world.project_a) == before
    last = world.records(world.project_a)[-1]
    assert (last.principal, last.action, last.reason) == (
        f"owner:{world.project_a}",
        Action.CONTRACT_APPROVE,
        Reason.SELF_APPROVAL,
    )
    world.verify_logs()


def test_atk_89_a_rotated_token_or_a_restarted_instance_is_still_the_proposer(
    restarted: World,
) -> None:
    world = restarted
    change_id = propose_note(world, "owner_a")
    from zephyros.core.ports.agent_engine import Side
    from zephyros.core.principals import instance_principal

    world.step("ATK-89.rotated token")
    world.tokens.revoke_instance("i_owner_a")
    renewed = world.tokens.issue_project_token(
        instance_principal(Side.OWNER, world.project_a), "i_owner_a", world["owner_a"].address, 3600
    )
    result = world["owner_a"].tool(
        "contract_approve",
        {"project_id": world.project_a, "change_id": change_id},
        token=renewed.secret,
    )
    assert result.refused
    assert result.error is not None
    assert result.error["reason"] == Reason.SELF_APPROVAL.value
    world.restart_hub()
    again = world["owner_a"].tool(
        "contract_approve",
        {"project_id": world.project_a, "change_id": change_id},
        token=renewed.secret,
    )
    assert again.error is not None
    assert again.error["reason"] == Reason.SELF_APPROVAL.value


def test_atk_89_the_contractor_cannot_approve_or_reject(restarted: World) -> None:
    world = restarted
    change_id = propose_note(world, "owner_a")
    before = world.fingerprint(world.project_a)
    for tool in ("contract_approve", "contract_reject"):
        result = world["contractor_a"].tool(
            tool, {"project_id": world.project_a, "change_id": change_id}
        )
        assert result.refused
    assert world.fingerprint(world.project_a) == before


def test_atk_89_positive_control_another_principal_approves_and_a_version_appears(
    restarted: World,
) -> None:
    world = restarted
    change_id = propose_note(world, "owner_a")
    world.step("ATK-89.positive")
    result = approve(world, "main", change_id)
    assert result.data is not None
    assert result.data["number"] == 2
    second = propose_note(world, "main", number=3)
    third = approve(world, "operator", second)
    assert third.data is not None
    assert third.data["number"] == 3
    log = [r for r in world.records(world.project_a) if r.action is Action.CONTRACT_APPROVE]
    assert [r.principal for r in log] == ["main-agent", "operator"]


# ----------------------------------------------------------------------------- US-E2-02
def test_us_e2_02_a_pending_change_leaves_the_version_alone_and_a_rejection_keeps_it(
    restarted: World,
) -> None:
    world = restarted
    version_before = world["owner_a"].tool("contract_read", {"project_id": world.project_a}).data
    assert version_before is not None
    change_id = propose_note(world, "owner_a")
    still = world["owner_a"].tool("contract_read", {"project_id": world.project_a}).data
    assert still is not None
    assert still["version_id"] == version_before["version_id"]
    assert "NB-002" not in still["objects"]
    rejected = world["main"].tool(
        "contract_reject", {"project_id": world.project_a, "change_id": change_id}
    )
    assert rejected.data is not None
    assert rejected.data["status"] == "rejected"
    after = world["owner_a"].tool("contract_read", {"project_id": world.project_a}).data
    assert after is not None
    assert after["version_id"] == version_before["version_id"]


def test_us_e2_02_an_approved_change_is_a_new_version_with_a_new_identifier(
    restarted: World,
) -> None:
    world = restarted
    first = world["owner_a"].tool("contract_read", {"project_id": world.project_a}).data
    approved = approve(world, "main", propose_note(world, "owner_a"))
    assert approved.data is not None
    assert approved.data["version_id"] != first["version_id"]  # type: ignore[index]  # reason: data set
    record = world.records(world.project_a)[-1]
    parameters = record.parameters
    assert "main-agent" in parameters
    assert approved.data["version_id"] in parameters
