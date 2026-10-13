"""The hub's client of the filter: what it sends, and how it reports what goes wrong."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from zephyros.adapters.filter.client import FilterClient
from zephyros.core.broker import BrokerError, parse_body, parse_request
from zephyros.core.containers import Role, VolumeKind
from zephyros.core.ports.agent_engine import Side

PROJECT = "p_abcdef12"
TOKEN = "zph_c_" + "T" * 43
INFO = {
    "role": "guard",
    "project_id": PROJECT,
    "side": "owner",
    "state": "running",
    "health": "healthy",
    "exit_code": None,
    "address": "10.200.0.18",
    "started_at": "2026-10-10T10:00:00Z",
    "finished_at": None,
}


def client_for(handler: Any) -> FilterClient:
    return FilterClient(httpx.Client(transport=httpx.MockTransport(handler), base_url="http://f"))


def ok(result: dict[str, Any]) -> Any:
    return lambda request: httpx.Response(200, json={"ok": True, "result": result})


def test_every_call_is_one_post_of_a_request_the_filter_accepts() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        parse_request(parse_body(request.content))  # the real parser accepts what we send
        op = json.loads(request.content)["op"]
        answers: dict[str, dict[str, Any]] = {
            "hub.connect": {"address": "10.200.0.19"},
            "container.inspect": {"container": None},
            "container.list": {"containers": [INFO]},
            "events": {"events": []},
        }
        result = answers.get(op, {})
        return httpx.Response(200, json={"ok": True, "result": result})

    client = client_for(handler)
    client.create_network(PROJECT, Side.OWNER, "10.200.0.16/28")
    assert client.connect_hub(PROJECT, Side.OWNER) == "10.200.0.19"
    client.disconnect_hub(PROJECT, Side.OWNER)
    client.create_volume(PROJECT, Side.OWNER, VolumeKind.HOME)
    client.remove_volume(PROJECT, Side.OWNER, VolumeKind.WORK)
    client.start_guard(PROJECT, Side.OWNER, "10.200.0.16/28")
    client.stop_guard(PROJECT, Side.OWNER)
    client.start_turn(PROJECT, Side.OWNER, TOKEN, "go")
    client.stop_turn(PROJECT, Side.OWNER)
    client.remove_network(PROJECT, Side.OWNER)
    assert client.inspect(Role.GUARD, PROJECT, Side.OWNER) is None
    assert [c.address for c in client.list_containers()] == ["10.200.0.18"]
    assert client.events(5) == []
    assert {(r.method, r.url.path) for r in seen} == {("POST", "/operation")}
    assert len(seen) == 13


def test_an_inspected_container_is_returned_as_data() -> None:
    client = client_for(ok({"container": INFO}))
    info = client.inspect(Role.GUARD, PROJECT, Side.OWNER)
    assert info is not None
    assert (info.role, info.health, info.exit_code, info.finished_at) == (
        "guard",
        "healthy",
        None,
        None,
    )


def test_a_refusal_keeps_the_filters_code_and_message() -> None:
    client = client_for(
        lambda request: httpx.Response(
            403, json={"ok": False, "error": {"code": "not_zephyros", "message": "no"}}
        )
    )
    with pytest.raises(BrokerError, match="no") as caught:
        client.stop_guard(PROJECT, Side.OWNER)
    assert caught.value.code == "not_zephyros"


def test_a_filter_that_cannot_be_reached_is_reported_without_details() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 2] /run/zephyros/filter.sock: " + TOKEN)

    with pytest.raises(BrokerError) as caught:
        client_for(handler).stop_guard(PROJECT, Side.OWNER)
    assert caught.value.code == "filter_unreachable"
    assert TOKEN not in str(caught.value)
    assert "ConnectError" in str(caught.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>"),
        httpx.Response(502, text="Bad Gateway"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"ok": False}),
        httpx.Response(200, json={"result": {}}),
    ],
)
def test_an_answer_that_is_not_the_filters_is_a_bad_reply(response: httpx.Response) -> None:
    with pytest.raises(BrokerError) as caught:
        client_for(lambda request: response).stop_guard(PROJECT, Side.OWNER)
    assert caught.value.code == "bad_reply"
