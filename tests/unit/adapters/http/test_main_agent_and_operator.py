"""The main-agent endpoint and the operator channel (ATK-13; US-E1-14; US-E2-02).

The operator channel is unreachable from an execution context at the network level (ADR 0025);
here we check what the listener itself refuses: tokens of the wrong kind, a browser's markers,
a foreign Host.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.unit.core.contract_fixtures import note, put, wire
from tests.unit.core.hub_fixtures import Env, make_env, seed_contract
from zephyros.adapters.http.main_agent import build_main_agent_app
from zephyros.adapters.http.operator import build_operator_app
from zephyros.core.audit import Action, Reason

PORT = 8081


@pytest.fixture
def env() -> Env:
    environment = make_env()
    seed_contract(environment)
    return environment


def main_client(env: Env, token: str | None, address: str = "172.18.0.1") -> TestClient:
    http = TestClient(build_main_agent_app(env.hub), client=(address, 41000))
    if token:
        http.headers["Authorization"] = f"Bearer {token}"
    return http


def operator_client(env: Env, token: str | None = None, host: str | None = None) -> TestClient:
    http = TestClient(
        build_operator_app(env.hub, port=PORT),
        base_url=f"http://{host or f'127.0.0.1:{PORT}'}",
        client=("172.18.0.1", 42000),
    )
    if token:
        http.headers["Authorization"] = f"Bearer {token}"
    return http


def operator_call(http: TestClient, tool: str, **arguments: Any) -> Any:
    return http.post("/operator/call", json={"tool": tool, "arguments": arguments})


# ----------------------------------------------------------------------------- main agent
def test_the_main_agent_works_on_its_own_endpoint(env: Env) -> None:
    http = main_client(env, env.secrets["main"])
    response = http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 200
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert "project_create" in names
    assert "contract_apply_reserved" not in names
    assert "log_read" not in names


def test_only_the_main_agent_token_opens_the_main_agent_endpoint(env: Env) -> None:
    for who in ("operator", "owner_a", "contractor_a", "owner_b"):
        http = main_client(env, env.secrets[who], env.addresses[who])
        assert (
            http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
        ), who
    assert (
        main_client(env, None)
        .post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        .status_code
        == 401
    )
    assert main_client(env, env.secrets["main"]).get("/anything").status_code == 404


def test_the_main_agent_proposes_but_cannot_apply_reserved_data(env: Env) -> None:
    http = main_client(env, env.secrets["main"])

    def tool(name: str, arguments: dict[str, Any]) -> Any:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        return http.post("/mcp", json=body).json()

    reserved = tool(
        "contract_apply_reserved",
        {"project_id": env.project_a, "summary": "x", "operations": [wire(put(note(5, "EXG-01")))]},
    )
    assert reserved["result"]["isError"] is True
    proposal = tool(
        "contract_propose",
        {"project_id": env.project_a, "summary": "x", "operations": [wire(put(note(5, "EXG-01")))]},
    )
    assert proposal["result"]["isError"] is False


# ----------------------------------------------------------------------------- operator
def test_the_operator_calls_a_tool_with_its_token(env: Env) -> None:
    http = operator_client(env, env.secrets["operator"])
    response = operator_call(http, "whoami")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {"principal": "operator", "kind": "operator", "side": None, "project_id": None},
    }
    created = operator_call(http, "project.create", name="From the CLI")
    assert created.json()["ok"] is True
    listed = operator_call(http, "project.list").json()["data"]["projects"]
    assert len(listed) == 3
    assert operator_client(env).get("/operator/health").json() == {"status": "ok"}


def test_every_use_of_the_operator_channel_is_in_the_log(env: Env) -> None:
    http = operator_client(env, env.secrets["operator"])
    operator_call(http, "contract.read", project_id=env.project_a)
    record = env.last(env.project_a)
    assert (record.principal, record.action) == ("operator", Action.CONTRACT_READ)


@pytest.mark.parametrize(
    "token_of",
    ["main", "owner_a", "contractor_a", None],
    ids=["main", "owner", "contractor", "none"],
)
def test_only_the_operator_token_opens_the_operator_channel(env: Env, token_of: str | None) -> None:
    http = operator_client(env, env.secrets[token_of] if token_of else None)
    assert operator_call(http, "whoami").status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://evil.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Mode": "cors"},
        {"Cookie": "session=abc"},
    ],
)
def test_a_request_with_a_browsers_markers_is_refused_before_the_token_is_read(
    env: Env, headers: dict[str, str]
) -> None:
    http = operator_client(env, env.secrets["operator"])
    response = http.post(
        "/operator/call", json={"tool": "whoami", "arguments": {}}, headers=headers
    )
    assert response.status_code == 403
    record = env.last("hub")
    assert (record.action, record.reason, record.principal) == (
        Action.OPERATOR_CHANNEL,
        Reason.BROWSER_REQUEST,
        "unauthenticated",
    )
    assert http.get("/operator/health", headers=headers).status_code == 403


@pytest.mark.parametrize(
    "host", ["evil.example", "evil.example:8081", "127.0.0.1:9999", "localhost", "10.0.0.5:8081"]
)
def test_a_request_for_another_host_is_refused_against_dns_rebinding(env: Env, host: str) -> None:
    http = operator_client(env, env.secrets["operator"], host=host)
    response = operator_call(http, "whoami")
    assert response.status_code == 403
    assert env.last("hub").reason is Reason.BAD_HOST


def test_the_hosts_of_the_operator_channel_are_the_loopback_ones(env: Env) -> None:
    for host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}"):
        http = operator_client(env, env.secrets["operator"], host=host)
        assert operator_call(http, "whoami").status_code == 200, host


def test_a_malformed_operator_request_is_refused_and_logged(env: Env) -> None:
    http = operator_client(env, env.secrets["operator"])
    bodies: list[object] = [[1], {"arguments": {}}, {"tool": "whoami", "extra": 1}, "text"]
    for body in bodies:
        assert http.post("/operator/call", json=body).status_code == 400
    assert (
        http.post(
            "/operator/call", content=b"{no", headers={"content-type": "application/json"}
        ).status_code
        == 400
    )
    assert (
        http.post(
            "/operator/call", content=b"x", headers={"content-type": "text/plain"}
        ).status_code
        == 415
    )
    assert env.last("hub").reason is Reason.INVALID_REQUEST


@pytest.mark.parametrize(
    ("tool", "arguments", "status"),
    [
        ("project.get", {"project_id": "p_9999999999"}, 404),
        ("nonexistent.tool", {}, 404),
        ("whoami", {"surprise": 1}, 400),
        ("contract.approve", {"project_id": "p_0000000001", "change_id": "c_missing1"}, 404),
    ],
)
def test_failures_are_mapped_to_http_statuses(
    env: Env, tool: str, arguments: dict[str, Any], status: int
) -> None:
    http = operator_client(env, env.secrets["operator"])
    response = operator_call(http, tool, **arguments)
    assert response.status_code == status
    assert response.json()["ok"] is False
    assert response.json()["error"]["kind"]


def test_a_conflict_and_a_forbidden_are_mapped(env: Env) -> None:
    http = operator_client(env, env.secrets["operator"])
    proposal = operator_call(
        http,
        "contract.propose",
        project_id=env.project_a,
        summary="n",
        operations=[wire(put(note(5, "EXG-01")))],
    ).json()["data"]
    self_approval = operator_call(
        http, "contract.approve", project_id=env.project_a, change_id=proposal["change_id"]
    )
    assert self_approval.status_code == 403
    assert self_approval.json()["error"]["reason"] == "self_approval"
    moved = operator_call(
        http,
        "contract.apply_reserved",
        project_id=env.project_a,
        summary="x",
        operations=[
            wire(
                put(
                    {
                        "type": "requirement",
                        "id": "EXG-77",
                        "text": "T.",
                        "priority": "P1",
                        "methods": ["test"],
                    }
                )
            )
        ],
    )
    assert moved.status_code == 200
    stale = operator_call(
        http, "contract.reject", project_id=env.project_a, change_id=proposal["change_id"]
    )
    assert stale.status_code == 200
    again = operator_call(
        http, "contract.reject", project_id=env.project_a, change_id=proposal["change_id"]
    )
    assert again.status_code == 409


def test_nothing_but_the_operator_routes_answers_on_the_operator_port(env: Env) -> None:
    http = operator_client(env, env.secrets["operator"])
    for path in ("/", "/mcp", "/v1/models", "/docs", "/openapi.json", "/admin"):
        assert http.get(path).status_code == 404
        assert http.post(path, json={}).status_code == 404


# ----------------------------------------------------------------------------- ADR 0025
POOL = "10.200.0.0/16"


@pytest.mark.parametrize("address", ["10.200.0.19", "10.200.255.3", "::ffff:10.200.4.3"])
def test_a_peer_of_an_instance_network_is_refused_before_its_token_is_read(
    env: Env, address: str
) -> None:
    """Whatever routed it there, a runtime without isolation between its networks or an absent
    guard, a connection from an instance address meets a refusal, logged (ATK-13)."""
    operator = TestClient(
        build_operator_app(env.hub, port=PORT, instance_pool=POOL),
        base_url=f"http://127.0.0.1:{PORT}",
        client=(address, 42000),
    )
    operator.headers["Authorization"] = f"Bearer {env.secrets['operator']}"
    assert operator.get("/operator/health").status_code == 403
    assert operator_call(operator, "whoami").status_code == 403
    main = TestClient(build_main_agent_app(env.hub, instance_pool=POOL), client=(address, 41000))
    main.headers["Authorization"] = f"Bearer {env.secrets['main']}"
    assert main.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 403
    refused = [r for r in env.records() if r.reason is Reason.WRONG_SOURCE]
    assert [r.action for r in refused] == [
        Action.OPERATOR_CHANNEL,
        Action.OPERATOR_CHANNEL,
        Action.AUTHENTICATION,
    ]


def test_the_publication_and_the_hubs_own_loopback_are_served(env: Env) -> None:
    for address in ("172.18.0.1", "127.0.0.1"):
        operator = TestClient(
            build_operator_app(env.hub, port=PORT, instance_pool=POOL),
            base_url=f"http://127.0.0.1:{PORT}",
            client=(address, 42000),
        )
        operator.headers["Authorization"] = f"Bearer {env.secrets['operator']}"
        assert operator_call(operator, "whoami").status_code == 200
        main = TestClient(
            build_main_agent_app(env.hub, instance_pool=POOL), client=(address, 41000)
        )
        main.headers["Authorization"] = f"Bearer {env.secrets['main']}"
        answer = main.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert answer.status_code == 200
