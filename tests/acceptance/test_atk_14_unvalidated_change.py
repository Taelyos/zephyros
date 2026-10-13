"""ATK-14: a change request alone changes nothing, and a validation that no longer holds is
refused.

Positions P1 (the main agent, the discussion channel) and P4. The channel proposes; the
contract stays unchanged until the Owner approves. The Contractor's approval is refused, the
proposer's own approval is refused, and a proposal whose base moved on — an expired validation
— cannot be approved any more. Every attempt is logged.
"""

from __future__ import annotations

import hashlib

import pytest

from tests.acceptance.world import World
from tests.unit.core.contract_fixtures import note, put, wire
from zephyros.core.audit import Outcome, Reason, canonical_json

pytestmark = pytest.mark.acceptance


def change(number: int) -> dict[str, object]:
    return {"summary": f"note {number}", "operations": [wire(put(note(number, "EXG-01")))]}


def applied(world: World, project: str) -> str:
    """O-STATE, applied side: a hash of the versions alone — a pending change is not applied."""
    store = world.hub._contracts._store  # reason: the oracle looks under the hub
    versions = [store.get(project, number) for number in store.numbers(project)]
    material = [(v.id, v.content_hash) for v in versions if v is not None]
    return hashlib.sha256(canonical_json(material).encode("ascii")).hexdigest()


def test_atk_14_a_change_needs_the_owners_fresh_validation(restarted: World) -> None:
    world = restarted
    project = world.project_a
    main, contractor, owner = world["main"], world["contractor_a"], world["owner_a"]
    before = applied(world, project)

    world.step("ATK-14.a propose")
    proposed = main.tool("contract_propose", {"project_id": project, **change(3)})
    assert not proposed.refused, proposed.error
    assert proposed.data is not None
    change_id = str(proposed.data["change_id"])
    world.step("ATK-14.b nothing applied without the validation")
    assert applied(world, project) == before

    world.step("ATK-14.c the Contractor cannot validate")
    refused = contractor.tool("contract_approve", {"project_id": project, "change_id": change_id})
    assert refused.refused
    assert world.records(project)[-1].outcome is Outcome.REFUSED
    assert applied(world, project) == before

    world.step("ATK-14.d the proposer cannot validate its own change")
    refused = main.tool("contract_approve", {"project_id": project, "change_id": change_id})
    assert refused.refused
    assert world.records(project)[-1].outcome is Outcome.REFUSED
    assert applied(world, project) == before

    world.step("ATK-14.e an expired validation is refused")
    second = main.tool("contract_propose", {"project_id": project, **change(4)})
    assert not second.refused, second.error
    assert second.data is not None
    approved = owner.tool(
        "contract_approve",
        {"project_id": project, "change_id": str(second.data["change_id"])},
    )
    assert not approved.refused, approved.error
    moved = applied(world, project)
    assert moved != before
    stale = owner.tool("contract_approve", {"project_id": project, "change_id": change_id})
    assert stale.refused
    assert stale.error is not None
    assert stale.error["reason"] == Reason.STALE_BASE.value
    assert world.records(project)[-1].outcome is Outcome.REFUSED
    assert applied(world, project) == moved

    world.step("ATK-14.f a fresh validation applies the change")
    third = main.tool("contract_propose", {"project_id": project, **change(5)})
    assert not third.refused, third.error
    assert third.data is not None
    approved = owner.tool(
        "contract_approve",
        {"project_id": project, "change_id": str(third.data["change_id"])},
    )
    assert not approved.refused, approved.error
    assert applied(world, project) != moved
    world.verify_logs()
