"""ATK-84, EXG-49: the masking replaces secrets in the log, the evidence report and the pull
request, and nothing else.

A word that ends with a key prefix is no key: `risk-management-module` holds `sk-` and
`src/disk-usage-monitoring.py` holds `sk-usage-monitoring`. Through the whole loop — the push
and its log, the report, the pull request opened then retitled after an inspection, the
verdict posted on it — such words come back as they were written, while the canaries the
Owner side writes into its verdict come back masked.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.acceptance.test_delivery_loop import BASE, REPOSITORY, deliver, status
from tests.acceptance.world import World
from tests.support.git_fixtures import make_forge_repository
from tests.unit.core.contract_fixtures import put, wire
from tests.unit.core.test_gate import contract
from zephyros.adapters.dummy.verification import AssertionRunner
from zephyros.adapters.git.forge import DirectoryForge
from zephyros.app.hub import DeliverySetup
from zephyros.core.audit import Action
from zephyros.core.masking import MASK

pytestmark = pytest.mark.acceptance

BRANCH = "risk-management-module"
DELIVERED = "src/disk-usage-monitoring.py"
ACCEPTANCE = "checks/usage.txt"
CONSTRAINT = {
    "type": "constraint",
    "id": "CTR-01",
    "text": "The monitor shall use only the standard library of Python.",
    "priority": "P0",
    "methods": ["inspection"],
}
CANARIES = [
    "sk-live-abcdefghijklmnop1234",  # pragma: allowlist secret (a canary of ATK-84)
    "ghp_abcdefghijklmnopqrstuvwxyz012345",  # pragma: allowlist secret (a canary of ATK-84)
    "zph_t_abcdefghijklmnopqrstuvwx",  # pragma: allowlist secret (a canary of ATK-84)
    "-----BEGIN OPENSSH PRIVATE KEY-----",  # pragma: allowlist secret (a canary of ATK-84)
]


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


def pull_request(forge_root: Path) -> dict[str, Any]:
    owner, _, name = REPOSITORY.partition("/")
    found: list[dict[str, Any]] = json.loads(
        (forge_root / owner / f"{name}.pulls.json").read_text()
    )
    assert len(found) == 1
    return found[0]


def judged_delivery(world: World) -> str:
    """A delivery of BRANCH that changes DELIVERED: its test passes, and its P0 constraint waits
    for the Owner's inspection, so the gate decides CONCERNS and the pull request opens."""
    objects = contract((1, "P0", ("test",), (f"{ACCEPTANCE}::test_usage",)))
    world.operator_tool(
        "contract.apply_reserved",
        project_id=world.project_a,
        summary="a monitor, and a constraint verified by inspection",
        operations=[wire(put(item)) for item in [*objects.values(), CONSTRAINT]],
    )
    world.operator_tool(
        "project.prepare", project_id=world.project_a, repository=REPOSITORY, branch="main"
    )
    world.run_jobs()
    world.operator_tool(
        "acceptance.put", project_id=world.project_a, path=ACCEPTANCE, content="test_usage: pass\n"
    )
    files = {
        **BASE,
        DELIVERED: b"import shutil\n\n\ndef usage():\n    return shutil.disk_usage('/')\n",
    }
    delivery = deliver(world, files, branch=BRANCH)
    assert status(world, delivery)["decision"] == "CONCERNS"
    return delivery


def test_atk_84_words_that_hold_a_key_prefix_come_back_intact_everywhere(
    world: World, forge_root: Path
) -> None:
    delivery = judged_delivery(world)
    assert pull_request(forge_root)["title"] == f"{BRANCH} (delivery {delivery}, CONCERNS)"
    observation = f"{DELIVERED} imports shutil alone, on {BRANCH}"
    inspected = world["owner_a"].tool(
        "inspection_record",
        {
            "delivery_id": delivery,
            "object_id": "CTR-01",
            "verdict": "conforms",
            "observation": observation,
        },
    )
    assert not inspected.refused, inspected.error
    reviewed = world["owner_a"].tool(
        "delivery_review",
        {"delivery_id": delivery, "verdict": "accept", "reasons": observation},
    )
    assert not reviewed.refused, reviewed.error
    world.run_jobs()

    # The pull request: retitled to the new decision, its body and its comments as written.
    found = pull_request(forge_root)
    assert found["title"] == f"{BRANCH} (delivery {delivery}, PASS)"
    assert f"`{DELIVERED}`" in found["body"]
    assert any(observation in comment for comment in found["comments"])
    assert MASK not in json.dumps(found)
    # The evidence report, with its addendum of inspections.
    report = status(world, delivery)["report"]
    assert f"`{DELIVERED}`" in report
    assert f"`{observation}`" in report
    assert MASK not in report
    # The log: the push names the branch as written, and no record holds a mask.
    records = world.records(world.project_a)
    pushes = [json.loads(r.parameters) for r in records if r.action.value == "delivery.push"]
    assert [push["branch"] for push in pushes] == [BRANCH]
    assert not [r for r in records if MASK in r.parameters]
    world.verify_logs()


def test_atk_84_canaries_in_a_message_and_a_verdict_come_back_masked(
    world: World, forge_root: Path
) -> None:
    """The canaries reach the log as the name of a tool the Contractor asks for (the log keeps
    it), and the pull request as the reasons of the Owner's verdict (posted on it)."""
    delivery = judged_delivery(world)
    for canary in CANARIES:
        assert world["contractor_a"].tool(canary).refused
    reasons = "the run printed " + " and ".join(CANARIES) + f", on {BRANCH}"
    reviewed = world["owner_a"].tool(
        "delivery_review",
        {"delivery_id": delivery, "verdict": "accept_with_reservations", "reasons": reasons},
    )
    assert not reviewed.refused, reviewed.error
    world.run_jobs()
    posted = json.dumps(pull_request(forge_root))
    records = world.records(world.project_a)
    asked = [r for r in records if r.action is Action.UNKNOWN]
    logged = json.dumps([r.parameters for r in records])
    for canary in CANARIES:
        assert canary not in posted
        assert canary not in logged
    # The canaries were really carried, and really masked; the branch next to them was not.
    assert [json.loads(r.parameters)["requested"] for r in asked] == [MASK] * len(CANARIES)
    assert f"and {MASK}, on {BRANCH}" in posted
    world.verify_logs()
