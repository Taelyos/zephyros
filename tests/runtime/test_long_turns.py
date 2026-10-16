"""AN-021 on a whole Zephyros (scripts/deploy.sh), with the obedient stub as the model.

The lifetime of a project token is cut to two minutes, the shortest the configuration takes. A
Contractor turn of Hermes keeps the hub busy for three: its last call of a tool of the hub still
goes through, since the hub renews the token of a turn while it runs. The turn keeps its token in
the work volume; the next turn presents it, and the operator sees the refusal: in the status of
the instance, and in the log as an event told to the main agent.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest

from tests.runtime.deployment import Deployment
from tests.unit.core.contract_fixtures import seed_operations, wire
from zephyros.core.ports.agent_engine import Side

pytestmark = [pytest.mark.runtime, pytest.mark.acceptance, pytest.mark.slow]

CONTRACTOR = Side.CONTRACTOR
LIFETIME = 120
TOKENS = f"[tokens]\nlifetime_seconds = {LIFETIME}\nrotate_before_seconds = 60\n"
OLD_TOKEN = "/work/first-token"


@pytest.fixture(scope="module")
def zephyros(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Deployment]:
    deployment = Deployment(
        tmp_path_factory.mktemp("deployment"), pool="10.229.0.0/16", extra_config=TOKENS
    )
    deployment.up()
    try:
        yield deployment
    finally:
        deployment.down()


def when(record: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(record["time"]).replace("Z", "+00:00"))


def of(records: list[dict[str, Any]], action: str, **matches: object) -> list[dict[str, Any]]:
    return [
        r
        for r in records
        if r.get("action") == action
        and all(r.get("parameters", {}).get(key) == value for key, value in matches.items())
    ]


def test_a_turn_longer_than_the_lifetime_of_a_token_keeps_its_tools_and_a_refusal_is_seen(
    zephyros: Deployment,
) -> None:
    project = zephyros.project([wire(o) for o in seed_operations()])
    read = f'TOOL mcp__zephyros__contract_read {{"project_id": "{project}"}}'
    long_turn = "\n".join(
        [
            f'RUN printf %s "$ZEPHYROS_TOKEN" > {OLD_TOKEN}',
            read,
            # three minutes of work, each step shorter than the timeout of the terminal of Hermes
            "RUN sleep 55",
            "RUN sleep 55",
            "RUN sleep 55",
            read,
        ]
    )
    assert zephyros.turn(project, CONTRACTOR, long_turn, seconds=480) == "completed"
    records = zephyros.records(project)
    [start] = of(records, "turn.start")
    reads = [r for r in of(records, "contract.read") if r["principal"] == f"contractor:{project}"]
    assert [r["outcome"] for r in reads] == ["ok", "ok"]
    assert (when(reads[-1]) - when(start)).total_seconds() > LIFETIME  # past the first lifetime
    assert of(records, "token.renew", side="contractor")
    assert not [r for r in of(records, "authentication") if r["outcome"] == "refused"]
    assert of(records, "token.revoke", side="contractor", why="turn_end")
    status = zephyros.data("instance.status", project_id=project, side="contractor")
    assert status["token_refused"] is None

    # The next turn presents the token of the first: it died with its turn.
    stale = "\n".join(
        [
            f'RUN curl -sS -o /work/refused.json -w "%{{http_code}}" -H "Authorization: Bearer '
            f'$(cat {OLD_TOKEN})" "${{ZEPHYROS_HUB_URL%/}}/staging/base"',
            read,
        ]
    )
    assert zephyros.turn(project, CONTRACTOR, stale, seconds=240) == "completed"
    status = zephyros.data("instance.status", project_id=project, side="contractor")
    refused = status["token_refused"]
    assert refused is not None, status
    assert (refused["reason"], refused["of_turn"]) == ("revoked", False)
    [issued, _second] = of(zephyros.records(project), "token.issue", side="contractor")
    assert refused["token_id"] == issued["parameters"]["token_id"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        records = zephyros.records(project)
        told = of(records, "notification.sent", event="instance.token_refused")
        if told:
            break
        time.sleep(2)
    assert told, [r["action"] for r in records]
    assert (told[0]["parameters"]["reason"], told[0]["parameters"]["of_turn"]) == (
        "revoked",
        False,
    )
    refusals = [r for r in of(records, "authentication") if r["outcome"] == "refused"]
    assert [r["reason"] for r in refusals] == ["revoked"]
    reads = [r for r in of(records, "contract.read") if r["principal"] == f"contractor:{project}"]
    assert [r["outcome"] for r in reads] == ["ok", "ok", "ok"]  # the second turn went on
