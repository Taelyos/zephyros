"""ADR 0033: a conversation at the point where the engines compact passes the gateway, over the
real HTTP stack, and reaches the provider whole; past the bound of memory, it does not.

The conversation is shaped like the real one the sizes were measured on: a system prompt and
25 tool definitions (55 000 bytes), then tool calls and their results, about 1 900 bytes a
message. Its size is the compaction point times 4.3 bytes a token, the larger of the two
ratios measured on that conversation (3.9 for the messages, 4.3 for the fixed part).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from tests.unit.core.hub_fixtures import make_env
from zephyros.adapters.http.agent import build_agent_app
from zephyros.adapters.providers.openai_compatible import FileCredential, OpenAiCompatibleProvider
from zephyros.core.context import DEFAULT_CONTEXT, AgentContext
from zephyros.core.gateway import (
    DEFAULT_LIMITS,
    MAX_BODY,
    GatewayLimits,
    GatewayService,
)

MODEL = "served-model"
MEASURED_BYTES_PER_TOKEN = 4.3
TOOL = {
    "type": "function",
    "function": {
        "name": "terminal",
        "description": "Run a command in the terminal of the work directory. " * 30,
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
    },
}
RESULT = (
    'def add(a: int, b: int) -> int:\n    """Add two numbers."""\n    return a + b\n\n' * 20
    + "tests/test_calc.py::test_add PASSED\n"
)


def conversation(size: int) -> dict[str, Any]:
    """A chat request of about `size` bytes, as an engine sends one."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "You are the Contractor of this project. " * 175},
        {"role": "user", "content": "Build the calculator of the contract."},
    ]
    body: dict[str, Any] = {"model": MODEL, "messages": messages, "tools": [TOOL] * 25}
    pair = len(
        json.dumps(
            [
                {"role": "assistant", "content": None, "tool_calls": [call(0)]},
                {"role": "tool", "tool_call_id": "call_0", "content": RESULT},
            ]
        )
    )
    for number in range(max(0, size - len(json.dumps(body))) // pair):
        messages.append({"role": "assistant", "content": None, "tool_calls": [call(number)]})
        messages.append({"role": "tool", "tool_call_id": f"call_{number}", "content": RESULT})
    return body


def call(number: int) -> dict[str, Any]:
    arguments = json.dumps({"command": f"pytest -q tests/test_calc.py -k case_{number}"})
    return {
        "id": f"call_{number}",
        "type": "function",
        "function": {"name": "terminal", "arguments": arguments},
    }


class Wire:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []


@pytest.fixture
def wire() -> Wire:
    return Wire()


def served(tmp_path: Path, wire: Wire, limits: GatewayLimits) -> Iterator[TestClient]:
    env = make_env()
    credential = tmp_path / "credential"
    credential.write_text("sk-test", encoding="utf-8")

    def transport(request: httpx.Request) -> httpx.Response:
        wire.sent.append(json.loads(request.content))
        answer = {"role": "assistant", "content": "done"}
        usage = {"prompt_tokens": 3, "completion_tokens": 1}
        return httpx.Response(200, json={"choices": [{"message": answer}], "usage": usage})

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    provider = OpenAiCompatibleProvider(
        "https://upstream.example.test/v1",
        FileCredential(credential),
        upstream_model="upstream",
        client=client,
    )
    gateway = GatewayService(audit=env.audit, provider=provider, model=MODEL, limits=limits)
    http = TestClient(
        build_agent_app(env.hub, model_name=MODEL, gateway=gateway),
        client=(env.addresses["contractor_a"], 40000),
    )
    http.headers["Authorization"] = f"Bearer {env.secrets['contractor_a']}"
    try:
        yield http
    finally:
        asyncio.run(client.aclose())


@pytest.mark.parametrize(
    "context",
    [DEFAULT_CONTEXT, AgentContext(1_000_000, 400_000), AgentContext(1_000_000, 1_000_000)],
    ids=["default", "1M-400k", "1M-whole"],
)
def test_a_conversation_at_the_compaction_point_passes_the_gateway_whole(
    tmp_path: Path, wire: Wire, context: AgentContext
) -> None:
    body = conversation(int(context.compaction * MEASURED_BYTES_PER_TOKEN))
    limits = GatewayLimits.for_window(context.window)
    for http in served(tmp_path, wire, limits):
        answer = http.post("/v1/chat/completions", json=body)
        assert answer.status_code == 200, answer.text
    [sent] = wire.sent
    assert len(sent["messages"]) == len(body["messages"])
    assert sent["messages"][-1] == body["messages"][-1]


@pytest.mark.parametrize(
    "context",
    [DEFAULT_CONTEXT, AgentContext(1_000_000, 400_000), AgentContext(1_000_000, 1_000_000)],
)
def test_the_limits_leave_room_past_the_compaction_point(context: AgentContext) -> None:
    """A turn may add more than one call to its conversation before the engine compacts:
    the body may grow well past the point."""
    limits = GatewayLimits.for_window(context.window)
    at_point = context.compaction * MEASURED_BYTES_PER_TOKEN
    assert limits.body_bytes >= 1.8 * at_point
    assert limits.characters >= 1.8 * at_point
    assert limits.messages >= 2 * at_point / 1_900  # messages of about 1 900 bytes


def test_the_former_limits_refused_a_conversation_of_400_000_tokens(
    tmp_path: Path, wire: Wire
) -> None:
    """Why the limits follow the window: the fixed 1 MiB cut the conversation of a window of a
    million tokens well before its compaction point."""
    body = conversation(int(400_000 * MEASURED_BYTES_PER_TOKEN))
    for http in served(tmp_path, wire, DEFAULT_LIMITS):
        assert http.post("/v1/chat/completions", json=body).status_code == 413
    assert wire.sent == []


def test_the_bound_of_memory_holds_whatever_the_window() -> None:
    assert GatewayLimits.for_window(10_000_000).body_bytes == MAX_BODY == 16 * 1024 * 1024
    assert GatewayLimits.for_window(64_000).body_bytes == 64_000 * 8 + 1024 * 1024
    assert GatewayLimits.for_window(10_000_000).messages == 100_000
    assert GatewayLimits.for_window(64_000).messages == 1000


def test_a_body_past_the_bound_is_refused_before_it_is_read_whole(
    tmp_path: Path, wire: Wire
) -> None:
    limits = GatewayLimits.for_window(1_000_000)
    for http in served(tmp_path, wire, limits):
        oversized = b"x" * (limits.body_bytes + 1)
        answer = http.post(
            "/v1/chat/completions",
            content=oversized,
            headers={"content-type": "application/json"},
        )
        assert answer.status_code == 413
    assert wire.sent == []
