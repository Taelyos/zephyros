"""ATK-09 and ATK-10: the Contractor's token cannot write the contract or the rules.

Position P1 (a Contractor instance). Oracles: O-REFUSE (an authorization error), O-STATE (the
fingerprint of the contract does not move), O-LOG (one record names the instance, the action and
the time). Positive control: the same token reads its own contract. Repeated after a restart of
the hub.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.acceptance.world import World
from tests.unit.core.contract_fixtures import note, put, requirement, wire
from zephyros.core.audit import Action, Outcome, Reason
from zephyros.core.hub import TOOL_SPECS

pytestmark = pytest.mark.acceptance

OBJECT = wire(put(requirement(9, "A requirement the Contractor wants."))) | {}
WRITE_TOOLS: dict[str, dict[str, Any]] = {
    "contract_propose": {"summary": "slip it in", "operations": [OBJECT]},
    "contract_approve": {"change_id": "c_00000001"},
    "contract_reject": {"change_id": "c_00000001"},
    "contract_apply_reserved": {"summary": "slip it in", "operations": [OBJECT]},
}


@pytest.mark.parametrize("tool", sorted(WRITE_TOOLS))
def test_atk_09_every_tool_that_writes_the_contract_refuses_a_contractor_token(
    restarted: World, tool: str
) -> None:
    world = restarted
    contractor = world["contractor_a"]
    before = world.fingerprint(world.project_a)
    world.step("ATK-09.a")
    result = contractor.tool(tool, {"project_id": world.project_a, **WRITE_TOOLS[tool]})
    assert result.refused
    assert result.error is not None
    assert (result.error["error"], result.error["reason"]) == ("forbidden", "forbidden")
    world.step("ATK-09.b")
    assert world.fingerprint(world.project_a) == before
    last = world.records(world.project_a)[-1]
    assert (last.principal, last.outcome, last.reason) == (
        f"contractor:{world.project_a}",
        Outcome.REFUSED,
        Reason.FORBIDDEN,
    )
    assert last.action.value == TOOL_SPECS[tool.replace("_", ".", 1)].action.value
    assert last.time
    world.step("ATK-09.positive-control")
    assert contractor.tool("contract_read", {"project_id": world.project_a}).data is not None
    world.verify_logs()


def test_atk_09_the_same_calls_through_every_other_door_of_the_agent_endpoint(
    restarted: World,
) -> None:
    world = restarted
    contractor = world["contractor_a"]
    before = world.fingerprint(world.project_a)
    doors = [
        ("POST", "/api/contract", {"id": "EXG-01"}),
        ("PUT", "/api/contract/EXG-01", {"text": "weaker"}),
        ("DELETE", "/api/contract/EXG-01", None),
        ("POST", "/operator/call", {"tool": "contract.apply_reserved", "arguments": {}}),
        ("POST", "/v1/contract", {}),
    ]
    for method, path, body in doors:
        world.step(f"ATK-09.door {method} {path}")
        response = contractor.http().request(method, path, json=body)
        assert response.status_code in (404, 405)
    assert world.fingerprint(world.project_a) == before


def test_atk_10_the_contractor_cannot_change_the_rules(restarted: World) -> None:
    """Methods, gate thresholds, change classes, budgets, the protected-network list, the
    approval policy: no tool offers to, and the ones nearest to it refuse."""
    world = restarted
    contractor = world["contractor_a"]
    before = world.fingerprint(world.project_a)
    log_before = len(world.records(world.project_a))
    attempts: list[tuple[str, dict[str, Any]]] = [
        ("config_set", {"key": "gate.p0_coverage", "value": 0}),
        ("configuration_change", {"limits": {"memory_mib": 999999}}),
        ("budget_raise", {"amount": 10**6}),
        ("network_protected_list", {"remove": ["10.0.0.0/8"]}),
        ("approval_policy", {"amend": False}),
        ("method_update", {"id": "EXG-01", "methods": ["inspection"]}),
        (
            "contract_propose",
            {
                "project_id": world.project_a,
                "summary": "relax",
                "operations": [
                    wire(put(requirement(1, "The system may do it.", "P0", ("inspection",))))
                ],
            },
        ),
    ]
    for number, (tool, arguments) in enumerate(attempts):
        world.step(f"ATK-10.{number}")
        result = contractor.tool(tool, arguments)
        assert result.refused, tool
    assert world.fingerprint(world.project_a) == before
    assert len(world.records(world.project_a)) == log_before + len(attempts)
    for record in world.records(world.project_a)[log_before:]:
        assert record.outcome is Outcome.REFUSED
        assert record.principal == f"contractor:{world.project_a}"
    world.verify_logs()


def test_atk_09_the_owner_side_is_refused_what_belongs_to_the_operator(restarted: World) -> None:
    """The Owner instance may propose; it may not apply reserved data (ATK-88 has the detail)."""
    world = restarted
    owner = world["owner_a"]
    result = owner.tool(
        "contract_apply_reserved",
        {
            "project_id": world.project_a,
            "summary": "x",
            "operations": [wire(put(note(9, "EXG-01")))],
        },
    )
    assert result.refused
    assert world.records(world.project_a)[-1].action is Action.CONTRACT_APPLY_RESERVED
