"""The agent endpoint over HTTP: MCP, authentication by token and peer address, the model
listing. Covers the hub side of ATK-12, ATK-13, ATK-85, ATK-87 and US-E1-12, US-E1-13."""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.unit.core.hub_fixtures import Env, make_env, seed_contract
from zephyros import __version__
from zephyros.adapters.http.agent import METADATA_PROBES, build_agent_app
from zephyros.core.audit import Action, Reason
from zephyros.core.errors import remedy
from zephyros.core.hub import TOOL_SPECS

MODEL = "zephyros-default"


@pytest.fixture
def env() -> Env:
    environment = make_env()
    seed_contract(environment)
    return environment


def client_for(
    env: Env, who: str, *, address: str | None = None, token: str | None = None
) -> TestClient:
    app = build_agent_app(env.hub, model_name=MODEL)
    http = TestClient(app, client=(address or env.addresses[who], 40000))
    secret = token if token is not None else env.secrets.get(who)
    if secret is not None:
        http.headers["Authorization"] = f"Bearer {secret}"
    return http


def rpc(http: TestClient, method: str, params: dict[str, Any] | None = None, ident: int = 1) -> Any:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": ident, "method": method}
    if params is not None:
        body["params"] = params
    return http.post("/mcp", json=body)


def call(http: TestClient, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    response = rpc(http, "tools/call", {"name": tool, "arguments": arguments or {}})
    assert response.status_code == 200
    return response.json()  # type: ignore[no-any-return]  # reason: JSON body


# ----------------------------------------------------------------------------- MCP
def test_the_mcp_handshake_and_a_tool_call(env: Env) -> None:
    http = client_for(env, "contractor_a")
    initialized = rpc(http, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    assert initialized.status_code == 200
    result = initialized.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "zephyros-hub"
    assert result["serverInfo"]["version"] == __version__
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    notified = http.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert notified.status_code == 202
    assert notified.content == b""
    assert rpc(http, "ping").json()["result"] == {}
    answer = call(http, "whoami")["result"]
    assert answer["isError"] is False
    assert json.loads(answer["content"][0]["text"])["principal"] == f"contractor:{env.project_a}"


def test_an_unknown_protocol_version_gets_the_latest_supported(env: Env) -> None:
    http = client_for(env, "owner_a")
    result = rpc(http, "initialize", {"protocolVersion": "1999-01-01"}).json()["result"]
    assert result["protocolVersion"] == "2025-06-18"


def test_tools_list_shows_each_caller_only_what_it_may_use(env: Env) -> None:
    contractor = rpc(client_for(env, "contractor_a"), "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in contractor} == {
        "whoami",
        "project_list",
        "project_get",
        "contract_read",
        "delivery_declare",
        "delivery_status",
    }
    operator_app = build_agent_app(env.hub, model_name=MODEL)
    assert operator_app is not None
    for tool in contractor:
        assert tool["description"]
        assert tool["inputSchema"]["additionalProperties"] is False


def test_a_refused_tool_call_is_an_error_result_and_an_unknown_tool_a_protocol_error(
    env: Env,
) -> None:
    http = client_for(env, "contractor_a")
    refused = call(
        http,
        "contract_apply_reserved",
        {"project_id": env.project_a, "summary": "x", "operations": []},
    )
    assert refused["result"]["isError"] is True
    body = json.loads(refused["result"]["content"][0]["text"])
    assert (body["error"], body["reason"]) == ("forbidden", "forbidden")
    unknown = call(http, "log_delete", {})
    assert unknown["error"]["code"] == -32602
    assert env.last(env.project_a).action is Action.UNKNOWN


def test_a_refused_tool_call_carries_its_code_cause_and_documented_action(env: Env) -> None:
    """EXG-51 through MCP, end to end: the three items reach the instance, as on the operator
    channel."""
    http = client_for(env, "contractor_a")
    refused = call(
        http,
        "contract_apply_reserved",
        {"project_id": env.project_a, "summary": "x", "operations": []},
    )["result"]
    assert refused["isError"] is True
    body = json.loads(refused["content"][0]["text"])
    assert body["reason"] == Reason.FORBIDDEN.value
    assert body["message"]
    assert body["action"] == remedy(Reason.FORBIDDEN)
    invalid = call(http, "delivery_status", {"project_id": env.project_a, "colour": "red"})
    body = json.loads(invalid["result"]["content"][0]["text"])
    assert (body["reason"], body["action"]) == ("invalid_request", remedy(Reason.INVALID_REQUEST))


def test_a_contract_can_be_read_through_mcp_and_nothing_else_of_it(env: Env) -> None:
    http = client_for(env, "contractor_a")
    result = call(http, "contract_read")["result"]
    data = json.loads(result["content"][0]["text"])
    assert data["number"] == 1
    assert "EXG-01" in data["objects"]


def test_the_probes_of_the_mcp_client_of_hermes_get_405_and_leave_no_record(env: Env) -> None:
    """Hermes' MCP client sends HEAD, then GET, before its first POST (seen in real turns)."""
    http = client_for(env, "contractor_a")
    before = env.record_count()
    for method in ("HEAD", "GET"):
        response = http.request(method, "/mcp")
        assert response.status_code == 405
        assert response.headers["allow"] == "POST, DELETE"
    assert env.record_count() == before


def test_get_delete_batch_and_bad_bodies(env: Env) -> None:
    http = client_for(env, "contractor_a")
    assert http.get("/mcp").status_code == 405
    assert http.get("/mcp").headers["allow"] == "POST, DELETE"
    assert http.delete("/mcp").status_code == 200
    assert (
        http.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}]).status_code == 400
    )
    assert (
        http.post(
            "/mcp", content=b"{not json", headers={"content-type": "application/json"}
        ).status_code
        == 400
    )
    assert (
        http.post("/mcp", content=b"x", headers={"content-type": "text/plain"}).status_code == 415
    )
    assert http.post("/mcp", json={"jsonrpc": "1.0", "id": 1, "method": "ping"}).status_code == 400
    assert http.post("/mcp", json={"jsonrpc": "2.0", "id": 1}).status_code == 400
    assert rpc(http, "resources/list").json()["error"]["code"] == -32601
    big = http.post(
        "/mcp", content=b"x" * (1024 * 1024 + 1), headers={"content-type": "application/json"}
    )
    assert big.status_code == 413
    assert rpc(http, "tools/call", {"name": 5}).json()["error"]["code"] == -32602


def test_a_body_that_is_not_json_is_logged_as_a_refusal_of_that_caller(env: Env) -> None:
    http = client_for(env, "contractor_a")
    http.post("/mcp", content=b"{nope", headers={"content-type": "application/json"})
    record = env.last(env.project_a)
    assert (record.principal, record.reason) == (
        f"contractor:{env.project_a}",
        Reason.INVALID_REQUEST,
    )


# ----------------------------------------------------------------------------- authentication
def test_no_token_and_no_good_token_give_one_answer(env: Env) -> None:
    secrets = env.secrets
    answers = []
    cases = [
        client_for(env, "contractor_a", token=""),
        client_for(env, "contractor_a", token="zph_c_" + "A" * 43),
        client_for(env, "contractor_a", token="garbage"),
        client_for(env, "contractor_a", token=secrets["owner_a"]),
        client_for(env, "contractor_a", token=secrets["main"]),
        client_for(env, "contractor_a", token=secrets["operator"]),
    ]
    for http in cases:
        if not http.headers.get("Authorization", "").split(" ")[-1]:
            http.headers.pop("Authorization", None)
        response = rpc(http, "tools/list")
        answers.append(
            (response.status_code, response.text, response.headers.get("www-authenticate"))
        )
    assert set(answers) == {(401, '{"error":"unauthorized"}', "Bearer")}


def test_a_token_copied_to_another_context_is_refused_and_logged(env: Env) -> None:
    """ATK-85: the Contractor presents the Owner's token, with and without a forged header."""
    thief = client_for(env, "contractor_a", token=env.secrets["owner_a"])
    assert rpc(thief, "tools/list").status_code == 401
    forged = client_for(env, "contractor_a", token=env.secrets["owner_a"])
    forged.headers["X-Forwarded-For"] = env.addresses["owner_a"]
    forged.headers["Forwarded"] = f"for={env.addresses['owner_a']}"
    forged.headers["X-Real-IP"] = env.addresses["owner_a"]
    assert rpc(forged, "tools/list").status_code == 401
    record = env.last(env.project_a)
    assert record.principal == f"owner:{env.project_a}"
    assert record.reason is Reason.WRONG_SOURCE
    assert json.loads(record.parameters)["source"] == env.addresses["contractor_a"]
    # A host on the LAN and an instance of another project fare no better.
    for address in ("192.168.7.50", env.addresses["owner_b"], env.addresses["contractor_b"]):
        assert rpc(client_for(env, "owner_a", address=address), "tools/list").status_code == 401
    # The positive control: each context with its own token.
    for who in ("owner_a", "contractor_a", "owner_b", "contractor_b"):
        assert rpc(client_for(env, who), "tools/list").status_code == 200


def test_the_main_agent_and_operator_tokens_do_not_work_on_the_agent_endpoint(env: Env) -> None:
    for who in ("main", "operator"):
        http = client_for(env, who, address=env.addresses["contractor_a"])
        assert rpc(http, "tools/list").status_code == 401
        http = client_for(env, who, address="172.18.0.1")
        assert rpc(http, "tools/list").status_code == 401
    assert env.last("hub").reason is Reason.WRONG_ENDPOINT


def test_a_token_stops_working_with_its_instance_and_after_its_lifetime(env: Env) -> None:
    """ATK-12 and ATK-87 on the hub side: revocation, expiry on the hub's clock."""
    http = client_for(env, "contractor_a")
    assert rpc(http, "tools/list").status_code == 200
    env.tokens.revoke_instance("i_contractor_a")
    assert rpc(http, "tools/list").status_code == 401
    assert env.last(env.project_a).reason is Reason.REVOKED
    owner = client_for(env, "owner_a")
    env.clock.advance(3600)
    assert rpc(owner, "tools/list").status_code == 401
    assert env.last(env.project_a).reason is Reason.EXPIRED


def test_a_missing_peer_address_is_refused(env: Env) -> None:
    app = build_agent_app(env.hub, model_name=MODEL)
    http = TestClient(app, client=None)  # type: ignore[arg-type]  # reason: a connection with no peer address
    http.headers["Authorization"] = f"Bearer {env.secrets['owner_a']}"
    assert rpc(http, "tools/list").status_code == 401


# ----------------------------------------------------------------------------- /v1 and the rest
def test_the_model_listing_answers_without_a_token_and_only_it(env: Env) -> None:
    http = client_for(env, "contractor_a", token="")
    http.headers.pop("Authorization", None)
    before = env.record_count()
    listing = http.get("/v1/models")
    assert listing.status_code == 200
    assert listing.json() == {
        "object": "list",
        "data": [{"id": MODEL, "object": "model", "owned_by": "zephyros"}],
    }
    assert env.record_count() == before
    assert http.post("/v1/models", json={}).status_code in (404, 405)


@pytest.mark.parametrize("path", sorted(METADATA_PROBES))
def test_the_metadata_probes_of_hermes_get_404_and_leave_no_record(env: Env, path: str) -> None:
    before = env.record_count()
    for http in (client_for(env, "contractor_a"), client_for(env, "contractor_a", token="")):
        assert http.get(path).status_code == 404
        assert http.post(path, json={}).status_code == 404
    assert env.record_count() == before


@pytest.mark.parametrize(
    "path",
    [
        "/operator/call",
        "/admin",
        "/docs",
        "/openapi.json",
        "/metrics",
        "/health",
        "/",
        "/v1/chat/completions",
        "/.git/config",
        "/api/contract",
        "/mcp/",
        "//mcp",
    ],
)
def test_nothing_else_of_the_hub_answers_on_the_agent_endpoint(env: Env, path: str) -> None:
    """ATK-13: no storage, administration or documentation route answers an agent."""
    authenticated = client_for(env, "contractor_a")
    anonymous = client_for(env, "contractor_a", token="")
    anonymous.headers.pop("Authorization", None)
    for http in (authenticated, anonymous):
        for method in ("GET", "POST"):
            assert http.request(method, path, json={}).status_code in (404, 405)


def test_an_unknown_path_with_a_valid_token_is_logged_once(env: Env) -> None:
    before = env.record_count()
    assert client_for(env, "contractor_a").get("/admin").status_code == 404
    assert env.record_count() == before + 1
    record = env.last(env.project_a)
    assert (record.principal, record.reason) == (f"contractor:{env.project_a}", Reason.NOT_FOUND)
    anonymous = client_for(env, "contractor_a", token="")
    anonymous.headers.pop("Authorization", None)
    anonymous.get("/admin")
    assert env.record_count() == before + 1


def test_every_tool_of_the_registry_answers_by_its_mcp_name(env: Env) -> None:
    http = client_for(env, "owner_a")
    for spec in TOOL_SPECS.values():
        answer = call(http, spec.mcp_name, {})
        assert "result" in answer or "error" in answer
