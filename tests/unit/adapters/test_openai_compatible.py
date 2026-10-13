"""The OpenAI-compatible provider: forwards, reads its credential at each call, leaks nothing."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx
import pytest

from zephyros.adapters.providers.openai_compatible import FileCredential, OpenAiCompatibleProvider
from zephyros.core.ports.model_provider import (
    ChatMessage,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    Role,
    ToolCall,
    ToolSpec,
)

Handler = Callable[[httpx.Request], httpx.Response]
KEY = "sk-canary-1234567890abcdef"
ECHO = ToolSpec("echo", "Repeat.", {"type": "object", "properties": {"text": {"type": "string"}}})


def request(*messages: ChatMessage) -> ModelRequest:
    return ModelRequest("zephyros-default", messages or (ChatMessage(Role.USER, "hi"),), (ECHO,))


def completion(
    message: Mapping[str, object] | None = None, usage: dict[str, int] | None = None
) -> dict[str, object]:
    return {
        "choices": [{"index": 0, "message": message or {"role": "assistant", "content": "ok"}}],
        "usage": usage or {"prompt_tokens": 5, "completion_tokens": 2},
    }


def provider_for(
    tmp_path: Path, handler: Handler, *, key: str = KEY
) -> tuple[OpenAiCompatibleProvider, Path]:
    path = tmp_path / "model.key"
    path.write_text(key + "\n", encoding="utf-8")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAiCompatibleProvider(
        "https://models.invalid/v1/",
        FileCredential(path),
        upstream_model="upstream-1",
        client=client,
    )
    return provider, path


def run(provider: OpenAiCompatibleProvider, model_request: ModelRequest) -> ModelResponse:
    async def main() -> ModelResponse:
        try:
            return await provider.complete(model_request)
        finally:
            await provider.aclose()

    return asyncio.run(main())


def test_the_conversation_is_forwarded_with_the_credential_and_the_upstream_model(
    tmp_path: Path,
) -> None:
    seen: dict[str, object] = {}

    def handler(http: httpx.Request) -> httpx.Response:
        seen["url"] = str(http.url)
        seen["auth"] = http.headers["authorization"]
        seen["body"] = json.loads(http.content)
        return httpx.Response(200, json=completion())

    provider, _path = provider_for(tmp_path, handler)
    calls = (ToolCall("c1", "echo", '{"text": "a"}'),)
    result = run(
        provider,
        request(
            ChatMessage(Role.USER, "hi"),
            ChatMessage(Role.ASSISTANT, None, calls),
            ChatMessage(Role.TOOL, "a", tool_call_id="c1"),
        ),
    )
    assert seen["url"] == "https://models.invalid/v1/chat/completions"
    assert seen["auth"] == f"Bearer {KEY}"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["model"] == "upstream-1"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "tool"]
    assert body["messages"][1]["tool_calls"][0]["function"]["name"] == "echo"
    assert body["messages"][2]["tool_call_id"] == "c1"
    assert body["tools"][0]["function"]["name"] == "echo"
    assert result.model == "zephyros-default"
    assert result.message.content == "ok"
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (5, 2)


def test_a_new_credential_is_used_by_the_next_call(tmp_path: Path) -> None:
    keys: list[str] = []

    def handler(http: httpx.Request) -> httpx.Response:
        keys.append(http.headers["authorization"])
        return httpx.Response(200, json=completion())

    provider, path = provider_for(tmp_path, handler)

    async def main() -> None:
        await provider.complete(request())
        path.write_text("sk-new-key-0000\n", encoding="utf-8")
        await provider.complete(request())
        await provider.aclose()

    asyncio.run(main())
    assert keys == [f"Bearer {KEY}", "Bearer sk-new-key-0000"]


def test_tool_calls_from_the_upstream_are_returned(tmp_path: Path) -> None:
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}
        ],
    }
    provider, _ = provider_for(tmp_path, lambda http: httpx.Response(200, json=completion(message)))
    assert run(provider, request()).message.tool_calls[0].name == "echo"


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
def test_an_upstream_error_is_reported_by_its_status_alone_never_its_body(
    tmp_path: Path, status: int
) -> None:
    provider, _ = provider_for(
        tmp_path,
        lambda http: httpx.Response(
            status, text=f"bad key {KEY} in {http.headers['authorization']}"
        ),
    )
    with pytest.raises(ModelProviderError) as caught:
        run(provider, request())
    assert str(status) in str(caught.value)
    assert KEY not in str(caught.value)


def test_an_unreachable_upstream_is_reported_without_details(tmp_path: Path) -> None:
    def handler(http: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect, Authorization: {http.headers['authorization']}")

    provider, _ = provider_for(tmp_path, handler)
    with pytest.raises(ModelProviderError) as caught:
        run(provider, request())
    assert KEY not in str(caught.value)
    assert "ConnectError" in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": []},
        {"nonsense": True},
        {"choices": [{"message": {"content": "x"}}]},
        {"choices": [{"message": {"content": "x"}}], "usage": {"prompt_tokens": "many"}},
        {
            "choices": [{"message": {"content": None}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    ],
)
def test_an_answer_that_is_not_a_chat_completion_is_refused(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    provider, _ = provider_for(tmp_path, lambda http: httpx.Response(200, json=payload))
    with pytest.raises(ModelProviderError, match="not a chat completion"):
        run(provider, request())


def test_a_non_json_answer_is_refused(tmp_path: Path) -> None:
    provider, _ = provider_for(tmp_path, lambda http: httpx.Response(200, text="<html>"))
    with pytest.raises(ModelProviderError, match="not a chat completion"):
        run(provider, request())


def test_a_tool_that_was_not_offered_is_refused(tmp_path: Path) -> None:
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "rm", "arguments": "{}"}}
        ],
    }
    provider, _ = provider_for(tmp_path, lambda http: httpx.Response(200, json=completion(message)))
    with pytest.raises(ModelProviderError, match="not offered"):
        run(provider, request())


def test_no_credential_and_no_conversation_are_refused_without_a_request(tmp_path: Path) -> None:
    def handler(http: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be sent")

    provider, path = provider_for(tmp_path, handler)
    with pytest.raises(ModelProviderError, match="empty"):
        run(provider, ModelRequest("m", ()))
    provider, path = provider_for(tmp_path, handler, key="")
    with pytest.raises(ModelProviderError, match="no model credential"):
        run(provider, request())
    missing = FileCredential(tmp_path / "absent")
    assert missing.read() == ""
    assert missing.secrets() == []
    assert FileCredential(path).secrets() == []


def test_a_session_header_is_sent_when_the_provider_asks_for_one(tmp_path: Path) -> None:
    """OpenCode Go refuses a call without `x-opencode-session`; the configuration names it."""
    seen: dict[str, str | None] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen["session"] = http_request.headers.get("x-opencode-session")
        return httpx.Response(200, json=completion())

    path = tmp_path / "model.key"
    path.write_text(KEY + "\n", encoding="utf-8")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAiCompatibleProvider(
        "https://models.invalid/v1/",
        FileCredential(path),
        upstream_model="upstream-1",
        session_header="x-opencode-session",
        client=client,
    )
    run(provider, request())
    assert seen["session"] is not None
    assert seen["session"].startswith("zephyros-")


def test_no_session_header_is_sent_when_none_is_configured(tmp_path: Path) -> None:
    seen: dict[str, str | None] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen["session"] = http_request.headers.get("x-opencode-session")
        return httpx.Response(200, json=completion())

    provider, _ = provider_for(tmp_path, handler)
    run(provider, request())
    assert seen["session"] is None
