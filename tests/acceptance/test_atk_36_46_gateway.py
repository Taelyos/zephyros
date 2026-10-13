"""US-E3-01, ATK-36 (gateway side) and ATK-46 (hub side): the model gateway and the credential.

The model credential lives in the hub. Whatever a provider says or fails with, nothing an
instance can read contains it. The half of ATK-46 that searches the container (environment,
files, history) runs against real containers in the tests marked `runtime`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.acceptance.world import MODEL, World
from zephyros.adapters.providers.openai_compatible import FileCredential, OpenAiCompatibleProvider
from zephyros.core.audit import Action, Outcome, Reason

pytestmark = pytest.mark.acceptance

CANARY = "-".join(["sk", "canary", "ZEPHYROS", "feedface", "cafe"])
ROTATED = "-".join(["sk", "rotated", "ZEPHYROS", "1111"])
HERMES_CALL: dict[str, Any] = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "Say hello"}],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "terminal",
                "description": "Run.",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
            },
        }
    ],
    "stream": False,
}


class Upstream:
    """A fake of the model service behind the gateway: it records the key it was sent and can be
    told to misbehave in the ways that might leak it."""

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.mode = "ok"

    def __call__(self, http: httpx.Request) -> httpx.Response:
        key = http.headers["authorization"].removeprefix("Bearer ")
        self.keys.append(key)
        message: dict[str, Any]
        if self.mode == "echo-key":
            message = {"role": "assistant", "content": f"your key is {key}"}
        elif self.mode == "error-with-key":
            return httpx.Response(500, text=f"internal error for key {key}")
        elif self.mode == "echo-header-in-tool-call":
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps({"command": f"echo {key}"}),
                        },
                    }
                ],
            }
        else:
            message = {"role": "assistant", "content": "Hello from the provider"}
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "message": message}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4},
            },
        )


@pytest.fixture
def served(tmp_path: Path) -> tuple[World, Upstream, Path]:
    key_file = tmp_path / "model.key"
    key_file.write_text(CANARY + "\n", encoding="utf-8")
    upstream = Upstream()
    credential = FileCredential(key_file)
    provider = OpenAiCompatibleProvider(
        "https://models.invalid/v1",
        credential,
        upstream_model="upstream-model",
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    (tmp_path / "world").mkdir()
    world = World(tmp_path / "world", provider=provider, credentials=credential)
    return world, upstream, key_file


def post(world: World, who: str, body: object, **options: Any) -> Any:
    return world[who].http(**options).post("/v1/chat/completions", json=body)


# ----------------------------------------------------------------------------- US-E3-01
def test_us_e3_01_an_instance_gets_the_answer_of_the_provider(
    served: tuple[World, Upstream, Path],
) -> None:
    world, upstream, _key = served
    for name in ("owner_a", "contractor_a", "owner_b"):
        world.step(f"US-E3-01.{name}")
        response = post(world, name, HERMES_CALL)
        assert response.status_code == 200
        body = response.json()
        assert body["choices"][0]["message"]["content"] == "Hello from the provider"
        assert body["usage"] == {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}
    assert upstream.keys == [CANARY] * 3  # the credential went upstream and nowhere else
    last = world.records(world.project_a)[-1]
    assert (last.principal, last.action, last.outcome) == (
        f"contractor:{world.project_a}",
        Action.GATEWAY_CALL,
        Outcome.OK,
    )
    assert json.loads(last.parameters)["prompt_tokens"] == 9


def test_us_e3_01_a_streamed_call_is_served_too(served: tuple[World, Upstream, Path]) -> None:
    world, _upstream, _key = served
    response = post(
        world,
        "contractor_a",
        {**HERMES_CALL, "stream": True, "stream_options": {"include_usage": True}},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [line for line in response.text.split("\n\n") if line]
    assert events[-1] == "data: [DONE]"
    assert (
        json.loads(events[1][len("data: ") :])["choices"][0]["delta"]["content"]
        == "Hello from the provider"
    )
    assert json.loads(events[-2][len("data: ") :])["usage"]["total_tokens"] == 13


def test_us_e3_01_when_the_credential_changes_the_next_call_uses_it(
    served: tuple[World, Upstream, Path],
) -> None:
    world, upstream, key_file = served
    post(world, "owner_a", HERMES_CALL)
    key_file.write_text(ROTATED + "\n", encoding="utf-8")
    world.step("US-E3-01.change")
    post(world, "owner_a", HERMES_CALL)
    assert upstream.keys == [CANARY, ROTATED]


def test_us_e3_01_the_gateway_needs_a_project_token_from_its_own_context(
    served: tuple[World, Upstream, Path],
) -> None:
    world, upstream, _key = served
    assert (
        world["owner_a"].http(token="").post("/v1/chat/completions", json=HERMES_CALL).status_code
        == 401
    )
    assert post(world, "owner_a", HERMES_CALL, address=world["owner_b"].address).status_code == 401
    for kind in ("main", "operator"):
        assert post(world, "owner_a", HERMES_CALL, token=world[kind].secret).status_code == 401
    assert upstream.keys == []


def test_us_e3_01_one_model_is_served(served: tuple[World, Upstream, Path]) -> None:
    world, upstream, _key = served
    response = post(world, "owner_a", {**HERMES_CALL, "model": "gpt-expensive"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"
    assert upstream.keys == []


# ----------------------------------------------------------------------------- ATK-46
@pytest.mark.parametrize("mode", ["echo-key", "error-with-key", "echo-header-in-tool-call"])
def test_atk_46_nothing_an_instance_can_read_holds_the_credential(
    served: tuple[World, Upstream, Path], mode: str
) -> None:
    world, upstream, _key = served
    upstream.mode = mode
    world.step(f"ATK-46.{mode}")
    seen: list[str] = []
    for name in ("owner_a", "contractor_a"):
        response = post(world, name, HERMES_CALL)
        seen.append(response.text)
        seen.append(str(dict(response.headers)))
    seen.append(world["contractor_a"].http().get("/v1/models").text)
    seen.append(world["contractor_a"].rpc("tools/list").text)
    seen.append(world["contractor_a"].tool("whoami").data.__repr__())
    for text in seen:
        assert CANARY not in text, text
    for stream in world.hub._audit.streams():  # reason: the oracle reads the log
        for record in world.records(stream):
            assert CANARY not in record.parameters
    if mode == "error-with-key":
        assert post(world, "owner_a", HERMES_CALL).status_code == 502
    world.verify_logs()


def test_atk_46_a_provider_error_is_a_generic_502_that_names_nothing(
    served: tuple[World, Upstream, Path],
) -> None:
    world, upstream, _key = served
    upstream.mode = "error-with-key"
    response = post(world, "owner_a", HERMES_CALL)
    assert response.status_code == 502
    assert response.json() == {
        "error": {
            "message": "the model provider failed",
            "type": "invalid_request_error",
            "code": "upstream_failure",
        }
    }
    record = world.records(world.project_a)[-1]
    assert record.outcome is Outcome.ERROR


def test_atk_46_no_route_of_the_hub_returns_a_configuration_or_a_secret(
    served: tuple[World, Upstream, Path],
) -> None:
    world, _upstream, _key = served
    for path in (
        "/v1/config",
        "/v1/credentials",
        "/v1/keys",
        "/v1/provider",
        "/config",
        "/env",
        "/.env",
        "/debug/vars",
    ):
        for method in ("GET", "POST"):
            response = world["contractor_a"].http().request(method, path, json={})
            assert response.status_code in (404, 405)
            assert CANARY not in response.text


# ----------------------------------------------------------------------------- ATK-36
def test_atk_36_a_token_of_project_b_with_a_header_naming_project_a_is_served_as_project_b(
    served: tuple[World, Upstream, Path],
) -> None:
    world, _upstream, _key = served
    client = world["contractor_b"].http()
    client.headers["X-Project-Id"] = world.project_a
    client.headers["OpenAI-Project"] = world.project_a
    response = client.post(f"/v1/chat/completions?project_id={world.project_a}", json=HERMES_CALL)
    assert response.status_code == 200
    assert world.records(world.project_b)[-1].action is Action.GATEWAY_CALL
    assert not [r for r in world.records(world.project_a) if r.action is Action.GATEWAY_CALL]


def test_atk_36_the_gateway_is_logged_per_caller_so_cost_has_an_owner(
    served: tuple[World, Upstream, Path],
) -> None:
    world, _upstream, _key = served
    post(world, "owner_a", HERMES_CALL)
    post(world, "owner_b", HERMES_CALL)
    calls_a = [r for r in world.records(world.project_a) if r.action is Action.GATEWAY_CALL]
    calls_b = [r for r in world.records(world.project_b) if r.action is Action.GATEWAY_CALL]
    assert [r.principal for r in calls_a] == [f"owner:{world.project_a}"]
    assert [r.principal for r in calls_b] == [f"owner:{world.project_b}"]
    assert Reason.NONE.value == "none"
