"""ATK-13 and ATK-33 (and the hub side of ATK-36): what an instance reaches of the hub, and of
another project.

ATK-13 is half a network attack: the connection to the operator channel and the main-agent
endpoint is blocked by the guard (tests marked `runtime`, Z2-08). Here is the half the hub
answers for: the agent endpoint serves nothing but the MCP and model routes, and the other two
listeners accept nothing from an instance's token.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.acceptance.world import World
from tests.unit.core.contract_fixtures import note, put, wire
from zephyros.core.audit import Outcome, Reason

pytestmark = pytest.mark.acceptance

SCOPED: list[tuple[str, dict[str, Any]]] = [
    ("project_get", {}),
    ("contract_read", {}),
    ("contract_read", {"version": 1}),
    ("contract_propose", {"summary": "x", "operations": [wire(put(note(7, "EXG-01")))]}),
    ("contract_approve", {"change_id": "c_00000001"}),
    ("contract_reject", {"change_id": "c_00000001"}),
]


# ----------------------------------------------------------------------------- ATK-13
@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/admin",
        "/dashboard",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/metrics",
        "/health",
        "/healthz",
        "/storage",
        "/git",
        "/.git/HEAD",
        "/contract",
        "/api",
        "/api/v1/contract",
        "/operator/call",
        "/operator/health",
        "/gateway/admin",
        "/v1/admin",
        "/v1/usage",
        "/debug",
        "/console",
    ],
)
def test_atk_13_the_agent_endpoint_answers_on_no_route_but_its_own(
    restarted: World, path: str
) -> None:
    world = restarted
    for name in ("contractor_a", "owner_a"):
        world.step(f"ATK-13.{path}")
        client = world[name].http()
        assert client.get(path).status_code in (404, 405)
        assert client.post(path, json={}).status_code in (404, 405)


def test_atk_13_the_operator_channel_and_the_main_agent_endpoint_take_no_instance_token(
    restarted: World,
) -> None:
    world = restarted
    from starlette.testclient import TestClient

    for name in ("contractor_a", "owner_a", "contractor_b", "owner_b"):
        secret = world[name].secret
        operator = TestClient(
            world.operator_app, base_url="http://127.0.0.1:8081", client=(world[name].address, 1)
        )
        operator.headers["Authorization"] = f"Bearer {secret}"
        assert (
            operator.post("/operator/call", json={"tool": "whoami", "arguments": {}}).status_code
            == 401
        )
        assert (
            world.main_client(token=secret, address=world[name].address)
            .post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            .status_code
            == 401
        )
    world.verify_logs()


def test_atk_13_positive_control_the_agent_endpoint_serves_the_mcp_route(restarted: World) -> None:
    world = restarted
    assert world["contractor_a"].rpc("tools/list").status_code == 200
    assert world["contractor_a"].http().get("/v1/models").status_code == 200


# ----------------------------------------------------------------------------- ATK-33
@pytest.mark.parametrize(("tool", "extra"), SCOPED)
@pytest.mark.parametrize("name", ["owner_b", "contractor_b"])
def test_atk_33_an_instance_of_project_b_cannot_name_project_a(
    restarted: World, name: str, tool: str, extra: dict[str, Any]
) -> None:
    world = restarted
    before = world.fingerprint(world.project_a)
    world.step(f"ATK-33.{tool}")
    result = world[name].tool(tool, {"project_id": world.project_a, **extra})
    assert result.refused
    assert world.fingerprint(world.project_a) == before
    last = world.records(world.project_b)[-1]
    assert last.principal == f"{name.split('_')[0]}:{world.project_b}"
    assert last.outcome is Outcome.REFUSED
    assert last.reason in (Reason.OTHER_PROJECT, Reason.FORBIDDEN)
    # nothing about project A reached the log of project B except the attempt itself
    assert not [r for r in world.records(world.project_a) if r.principal.endswith(world.project_b)]


def test_atk_33_the_project_in_a_path_a_body_or_a_header_is_not_an_identity(
    restarted: World,
) -> None:
    world = restarted
    attacker = world["contractor_b"]
    client = attacker.http()
    client.headers["X-Project-Id"] = world.project_a
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "contract_read",
            "arguments": {},
            "project_id": world.project_a,
            "_meta": {"project_id": world.project_a},
        },
    }
    answer = client.post(f"/mcp?project_id={world.project_a}", json=body).json()["result"]
    assert answer["isError"] is False
    import json

    assert json.loads(answer["content"][0]["text"])["project_id"] == world.project_b


def test_atk_33_positive_control_the_same_calls_with_the_callers_own_project(
    restarted: World,
) -> None:
    world = restarted
    world.import_contract(world.project_b)
    for name in ("owner_b", "contractor_b"):
        for tool, extra in SCOPED[:3]:
            world.step(f"ATK-33.positive {tool}")
            result = world[name].tool(tool, {"project_id": world.project_b, **extra})
            assert result.data is not None, (name, tool, result.error)


def test_atk_33_the_list_of_projects_shows_an_instance_only_its_own(restarted: World) -> None:
    world = restarted
    listed = world["contractor_b"].tool("project_list").data
    assert listed is not None
    assert [p["project_id"] for p in listed["projects"]] == [world.project_b]


# ----------------------------------------------------------------------------- ATK-36 (hub side)
def test_atk_36_project_tokens_cross_no_project_on_any_route(restarted: World) -> None:
    world = restarted
    for victim, attacker in (
        ("a", "owner_b"),
        ("a", "contractor_b"),
        ("b", "owner_a"),
        ("b", "contractor_a"),
    ):
        project = world.project_a if victim == "a" else world.project_b
        result = world[attacker].tool("contract_read", {"project_id": project})
        assert result.refused, attacker
        gateway = world[attacker].http().get(f"/v1/projects/{project}/models")
        assert gateway.status_code in (404, 405)


# ----------------------------------------------------------------------------- operator channel
@pytest.mark.parametrize(
    "headers",
    [{"Origin": "http://evil.example"}, {"Sec-Fetch-Site": "cross-site"}, {"Cookie": "session=1"}],
)
def test_atk_13_the_operator_channel_refuses_a_request_that_looks_like_a_browsers(
    restarted: World, headers: dict[str, str]
) -> None:
    """A web page the human opens cannot drive the operator channel, even with the right token."""
    world = restarted
    before = world.fingerprint(world.project_a)
    response = world.operator_client().post(
        "/operator/call",
        json={"tool": "project.create", "arguments": {"name": "from a page"}},
        headers=headers,
    )
    assert response.status_code == 403
    assert world.fingerprint(world.project_a) == before
    listed = world["main"].tool("project_list").data
    assert listed is not None
    assert "from a page" not in [p["name"] for p in listed["projects"]]


@pytest.mark.parametrize("host", ["evil.example:8081", "rebound.example", "10.0.0.5:8081"])
def test_atk_13_the_operator_channel_refuses_a_foreign_host_name(
    restarted: World, host: str
) -> None:
    """DNS rebinding: a name that now points at the loopback is still not the operator's host."""
    world = restarted
    client = world.operator_client()
    response = client.post(
        "/operator/call", json={"tool": "whoami", "arguments": {}}, headers={"Host": host}
    )
    assert response.status_code == 403
