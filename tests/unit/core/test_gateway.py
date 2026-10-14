"""The model gateway core: the wire format, the rights, the log, the credential (US-E3-01)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from typing import Any

import pytest

from tests.unit.core.hub_fixtures import Env, make_env
from zephyros.adapters.dummy import ScriptedModelProvider
from zephyros.core.audit import Action, Outcome, Reason
from zephyros.core.gateway import (
    GatewayError,
    GatewayService,
    parse_chat_request,
    redact,
    render_chat_response,
    render_stream,
)
from zephyros.core.ports.model_provider import (
    ChatMessage,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    Role,
    ToolCall,
    Usage,
)

MODEL = "zephyros-default"

# The shape of what the pinned Hermes Agent (v0.21.6) sends.
HERMES_REQUEST: dict[str, Any] = {
    "model": MODEL,
    "messages": [
        {"role": "system", "content": "You are a Contractor."},
        {"role": "user", "content": "Say hello"},
    ],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "terminal",
                "description": "Run a command.",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
            },
        }
    ],
    "stream": True,
    "stream_options": {"include_usage": True},
    "temperature": 0.3,
    "parallel_tool_calls": True,
}


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


# ----------------------------------------------------------------------------- parsing
def test_a_request_shaped_like_hermes_is_understood() -> None:
    request = parse_chat_request(HERMES_REQUEST, model=MODEL)
    assert [m.role for m in request.messages] == [Role.SYSTEM, Role.USER]
    assert [t.name for t in request.tools] == ["terminal"]
    assert request.tools[0].parameters["type"] == "object"


def test_tool_calls_and_tool_results_round_trip_into_the_conversation() -> None:
    body = {
        "model": MODEL,
        "messages": [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "terminal", "arguments": '{"command": "id"}'},
                    },
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {"name": "terminal", "arguments": {"command": "ls"}},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "uid=10000"},
            {"role": "developer", "content": "be brief"},
        ],
    }
    request = parse_chat_request(body, model=MODEL)
    assert [c.id for c in request.messages[1].tool_calls] == ["c1", "c2"]
    assert json.loads(request.messages[1].tool_calls[1].arguments) == {"command": "ls"}
    assert request.messages[2].tool_call_id == "c1"
    assert request.messages[3].role is Role.SYSTEM


def test_text_parts_are_joined_and_other_parts_refused() -> None:
    body = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
            }
        ],
    }
    assert parse_chat_request(body, model=MODEL).messages[0].content == "ab"
    image = {
        "model": MODEL,
        "messages": [
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://x"}}]}
        ],
    }
    with pytest.raises(GatewayError) as caught:
        parse_chat_request(image, model=MODEL)
    assert caught.value.code == "unsupported_content"


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ("text", 400, "invalid_request"),
        (
            {"model": "other", "messages": [{"role": "user", "content": "x"}]},
            404,
            "model_not_found",
        ),
        ({"model": MODEL}, 400, "invalid_request"),
        ({"model": MODEL, "messages": []}, 400, "invalid_request"),
        ({"model": MODEL, "messages": "hi"}, 400, "invalid_request"),
        ({"model": MODEL, "messages": ["hi"]}, 400, "invalid_message"),
        ({"model": MODEL, "messages": [{"role": "root", "content": "x"}]}, 400, "invalid_message"),
        ({"model": MODEL, "messages": [{"role": "user"}]}, 400, "invalid_message"),
        ({"model": MODEL, "messages": [{"role": "tool", "content": "x"}]}, 400, "invalid_message"),
        ({"model": MODEL, "messages": [{"role": "user", "content": 5}]}, 400, "invalid_content"),
        (
            {"model": MODEL, "messages": [{"role": "assistant", "tool_calls": ["x"]}]},
            400,
            "invalid_tool_call",
        ),
        (
            {"model": MODEL, "messages": [{"role": "user", "content": "x"}], "tools": "t"},
            400,
            "invalid_tools",
        ),
        (
            {
                "model": MODEL,
                "messages": [{"role": "user", "content": "x"}],
                "tools": [{"type": "function"}],
            },
            400,
            "invalid_tools",
        ),
        (
            {
                "model": MODEL,
                "messages": [{"role": "user", "content": "x"}],
                "tools": [{"function": {"name": "t", "parameters": 5}}],
            },
            400,
            "invalid_tools",
        ),
        (
            {"model": MODEL, "messages": [{"role": "user", "content": "x" * 1_000_001}]},
            413,
            "too_large",
        ),
        (
            {"model": MODEL, "messages": [{"role": "user", "content": "x"}] * 1001},
            400,
            "too_many_messages",
        ),
    ],
)
def test_a_malformed_request_is_refused_with_an_openai_style_error(
    body: object, status: int, code: str
) -> None:
    with pytest.raises(GatewayError) as caught:
        parse_chat_request(body, model=MODEL)
    assert (caught.value.status, caught.value.code) == (status, code)
    assert caught.value.body()["error"]["code"] == code  # type: ignore[index]  # reason: shape asserted


# ----------------------------------------------------------------------------- rendering
def response(*, content: str | None = "hi", calls: tuple[ToolCall, ...] = ()) -> ModelResponse:
    return ModelResponse(MODEL, ChatMessage(Role.ASSISTANT, content, calls), Usage(7, 3))


def test_a_plain_answer_is_rendered_as_a_chat_completion() -> None:
    rendered = render_chat_response(response(), created=1, identifier="chatcmpl-1")
    assert rendered["choices"][0]["message"] == {"role": "assistant", "content": "hi"}
    assert rendered["choices"][0]["finish_reason"] == "stop"
    assert rendered["usage"] == {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}


def test_a_tool_call_answer_is_rendered_with_its_calls() -> None:
    call = ToolCall("c1", "terminal", '{"command": "id"}')
    rendered = render_chat_response(
        response(content=None, calls=(call,)), created=1, identifier="x"
    )
    choice = rendered["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"] == [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "terminal", "arguments": '{"command": "id"}'},
        }
    ]


def test_a_streamed_answer_is_delta_then_end_then_usage_then_done() -> None:
    completion = render_chat_response(response(), created=1, identifier="x")
    events = render_stream(completion, include_usage=True)
    assert events[-1] == "data: [DONE]\n\n"
    payloads = [json.loads(e[len("data: ") :]) for e in events[:-1]]
    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert payloads[1]["choices"][0]["delta"] == {"content": "hi"}
    assert payloads[2]["choices"][0]["finish_reason"] == "stop"
    assert payloads[3]["usage"]["total_tokens"] == 10
    assert len(render_stream(completion, include_usage=False)) == 4
    call = ToolCall("c1", "terminal", "{}")
    tools = render_stream(
        render_chat_response(response(content=None, calls=(call,)), created=1, identifier="x"),
        include_usage=False,
    )
    assert (
        json.loads(tools[1][len("data: ") :])["choices"][0]["delta"]["tool_calls"][0]["index"] == 0
    )


def test_redaction_replaces_every_occurrence_of_a_secret() -> None:
    assert (
        redact("key=sk-1 and sk-1 and sk-2", ["sk-1", "sk-2", ""])
        == "key=[redacted] and [redacted] and [redacted]"
    )
    assert redact("nothing", []) == "nothing"


# ----------------------------------------------------------------------------- the service
class Credentials:
    def __init__(self, *values: str) -> None:
        self.values = list(values)

    def secrets(self) -> list[str]:
        return self.values


def make_service(env: Env, provider: Any, credentials: Credentials | None = None) -> GatewayService:
    return GatewayService(audit=env.audit, provider=provider, model=MODEL, credentials=credentials)


def reply(text: str = "answer", calls: tuple[ToolCall, ...] = ()) -> ChatMessage:
    return ChatMessage(Role.ASSISTANT, text if not calls else None, calls)


def test_an_instance_gets_the_providers_answer_and_one_record_is_written() -> None:
    env = make_env()
    provider = ScriptedModelProvider([reply("Hello from the provider")])
    service = make_service(env, provider)
    before = len(env.audit.read(env.project_a))
    answer = run(service.complete(env.callers["contractor_a"], HERMES_REQUEST))
    assert answer.completion["choices"][0]["message"]["content"] == "Hello from the provider"
    assert answer.stream is True
    assert answer.include_usage is True
    records = env.audit.read(env.project_a)
    assert len(records) == before + 1
    record = records[-1]
    assert (record.principal, record.action, record.outcome) == (
        f"contractor:{env.project_a}",
        Action.GATEWAY_CALL,
        Outcome.OK,
    )
    details = json.loads(record.parameters)
    assert details["model"] == MODEL
    assert details["prompt_tokens"] > 0
    assert provider.requests[0].model == MODEL
    assert env.audit.verify(env.project_a).ok


def test_the_operator_and_the_main_agent_have_no_model_calls() -> None:
    env = make_env()
    service = make_service(env, ScriptedModelProvider([reply()]))
    for who in ("operator", "main"):
        with pytest.raises(GatewayError) as caught:
            run(service.complete(env.callers[who], HERMES_REQUEST))
        assert (caught.value.status, caught.value.reason) == (403, Reason.FORBIDDEN)
    assert env.audit.read("hub")[-1].reason is Reason.FORBIDDEN


def test_a_bad_request_is_refused_and_logged_without_calling_the_provider() -> None:
    env = make_env()
    provider = ScriptedModelProvider([reply()])
    service = make_service(env, provider)
    with pytest.raises(GatewayError):
        run(service.complete(env.callers["owner_a"], {"model": "other", "messages": []}))
    assert provider.requests == []
    record = env.audit.read(env.project_a)[-1]
    assert (record.outcome, record.reason) == (Outcome.REFUSED, Reason.INVALID_REQUEST)


class Failing:
    interface_version = "0.1.0"

    def __init__(self, message: str) -> None:
        self.message = message

    async def complete(self, _request: ModelRequest) -> ModelResponse:
        raise ModelProviderError(self.message)

    async def aclose(self) -> None:
        return None


def test_a_provider_failure_gives_a_generic_502_and_leaks_nothing() -> None:
    env = make_env()
    canary = "-".join(["sk", "canary", "0123456789"])
    service = make_service(env, Failing(f"401 for key {canary}"), Credentials(canary))
    with pytest.raises(GatewayError) as caught:
        run(service.complete(env.callers["owner_a"], HERMES_REQUEST))
    assert caught.value.status == 502
    assert canary not in json.dumps(caught.value.body())
    record = env.audit.read(env.project_a)[-1]
    assert record.outcome is Outcome.ERROR
    assert canary not in record.parameters
    assert "[redacted]" in record.parameters


def test_a_credential_in_an_answer_never_reaches_the_instance() -> None:
    env = make_env()
    canary = "-".join(["sk", "canary", "abcdef"])
    service = make_service(
        env, ScriptedModelProvider([reply(f"the key is {canary}")]), Credentials(canary)
    )
    answer = run(service.complete(env.callers["owner_a"], HERMES_REQUEST))
    assert canary not in json.dumps(answer.completion)
    assert "[redacted]" in json.dumps(answer.completion)


def test_a_service_without_credentials_still_serves() -> None:
    env = make_env()
    service = make_service(env, ScriptedModelProvider([reply()]))
    assert (
        run(service.complete(env.callers["owner_a"], {**HERMES_REQUEST, "stream": False})).stream
        is False
    )
