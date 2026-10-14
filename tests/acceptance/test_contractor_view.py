"""T07 (I2): the Contractor reads the outcome of each acceptance test of the Owner side, never
the output of the run, which shows their source.

The real kit prints the traceback of a test that fails, and pytest's traceback quotes the
lines of the test around the failing assertion. The runner here does the same with the files
of the assertions kit: the output of the run quotes each file whose test failed. Whoever asks
`delivery.status` gets the report it may read (`report_for`).
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.acceptance.test_delivery_loop import (
    ADD_SUB,
    BASE,
    REPOSITORY,
    deliver,
    make_world,
    prepared,
)
from tests.acceptance.world import World
from tests.support.git_fixtures import make_forge_repository
from zephyros.adapters.dummy.verification import AssertionRunner
from zephyros.adapters.sqlite.delivery_store import SqliteDeliveryStore
from zephyros.core.deliveries import DeliveryRecord, DeliveryState
from zephyros.core.ports.verification import Outcome, VerificationRequest, VerificationResult

pytestmark = pytest.mark.acceptance

HIDDEN = "assert sub(7, 3) == 4  # the Owner side's hidden check"
"""A line of the Owner side's test that only its source shows."""


class TracebackRunner(AssertionRunner):
    """The assertions runner, whose output quotes the file of each test that failed, as the
    traceback of a real kit quotes the test."""

    def run(self, request: VerificationRequest) -> VerificationResult:
        result = super().run(request)
        quoted = [
            request.files.get(test.partition("::")[0], b"").decode("utf-8", errors="replace")
            for test, outcome in result.outcomes.items()
            if outcome is Outcome.FAILED
        ]
        return VerificationResult(result.outcomes, "\n".join([result.log, *quoted]))


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
    return make_world(tmp_path, forge_root, TracebackRunner())


def report_of(world: World, who: str, delivery: str) -> Any:
    found = world[who].tool("delivery_status", {"delivery_id": delivery})
    assert not found.refused, found.error
    assert found.data is not None
    return found.data["report"]


def test_the_contractor_reads_the_outcomes_and_not_the_owner_sides_test(world: World) -> None:
    prepared(world, put_sub=False)
    world.operator_tool(
        "acceptance.put",
        project_id=world.project_a,
        path="checks/sub.txt",
        content=f"test_sub: fail\n{HIDDEN}\n",
    )
    delivery = deliver(world, ADD_SUB)
    owner = report_of(world, "owner_a", delivery)
    operator = world.operator_tool(
        "delivery.status", project_id=world.project_a, delivery_id=delivery
    )["report"]
    contractor = report_of(world, "contractor_a", delivery)
    # the Owner side and the operator read the report whole, the output of the run included
    assert HIDDEN in owner
    assert HIDDEN in operator
    # the Contractor reads which test failed, not what the test checks
    assert HIDDEN not in contractor
    assert "checks/sub.txt::test_sub" in contractor
    assert "FAIL" in contractor


def test_a_record_kept_before_the_contractors_report_existed_shows_it_none(world: World) -> None:
    """A record written before the hub kept a report for the Contractor has only the whole
    one: the Contractor gets none rather than that one."""
    store = SqliteDeliveryStore(world.database)
    store.put_record(
        DeliveryRecord(
            project_id=world.project_a,
            delivery_id="d7",
            branch="old",
            state=DeliveryState.DECIDED,
            received_at="2026-10-12T08:00:00.000000Z",
            decision="FAIL",
            outcomes={"checks/sub.txt::test_sub": "failed"},
            report=f"the evidence report\n{HIDDEN}\n",
            contract_version=1,
        )
    )
    assert report_of(world, "contractor_a", "d7") is None
    assert HIDDEN in report_of(world, "owner_a", "d7")
