"""The scripted model provider keeps its own promises."""

from __future__ import annotations

import asyncio

import pytest

from zephyros.adapters.dummy import ScriptedModelProvider
from zephyros.core.ports.model_provider import (
    ChatMessage,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    Role,
    ToolCall,
    ToolSpec,
)

USER = (ChatMessage(Role.USER, "say something please"),)
ECHO = ToolSpec("echo", "Repeat.", {})


def _complete(provider: ScriptedModelProvider, request: ModelRequest) -> ModelResponse:
    return asyncio.run(provider.complete(request))


def test_replays_the_replies_in_order_and_records_the_requests() -> None:
    provider = ScriptedModelProvider(
        [ChatMessage(Role.ASSISTANT, "one"), ChatMessage(Role.ASSISTANT, "two")]
    )
    request = ModelRequest("m", USER)

    async def two_calls() -> list[str | None]:
        return [(await provider.complete(request)).message.content for _ in range(2)]

    assert asyncio.run(two_calls()) == ["one", "two"]
    assert provider.requests == [request, request]


def test_counts_words_as_usage() -> None:
    provider = ScriptedModelProvider([ChatMessage(Role.ASSISTANT, "three words here")])
    usage = _complete(provider, ModelRequest("m", USER)).usage
    assert (usage.prompt_tokens, usage.completion_tokens) == (3, 3)


def test_refuses_a_request_after_the_last_reply() -> None:
    provider = ScriptedModelProvider([])
    with pytest.raises(ModelProviderError, match="no reply left"):
        _complete(provider, ModelRequest("m", USER))


def test_refuses_an_empty_conversation_without_using_a_reply() -> None:
    provider = ScriptedModelProvider([ChatMessage(Role.ASSISTANT, "kept")])
    with pytest.raises(ModelProviderError, match="empty"):
        _complete(provider, ModelRequest("m", ()))
    assert _complete(provider, ModelRequest("m", USER)).message.content == "kept"


def test_refuses_a_reply_that_calls_a_tool_not_offered_and_keeps_the_reply() -> None:
    call = ToolCall("c1", "echo", '{"text": "hi"}')
    provider = ScriptedModelProvider([ChatMessage(Role.ASSISTANT, None, (call,))])
    with pytest.raises(ModelProviderError, match="not offered"):
        _complete(provider, ModelRequest("m", USER))
    answer = _complete(provider, ModelRequest("m", USER, (ECHO,)))
    assert answer.message.tool_calls == (call,)
    assert answer.usage.completion_tokens == 3


def test_a_script_holds_only_assistant_replies() -> None:
    with pytest.raises(ValueError, match="assistant"):
        ScriptedModelProvider([ChatMessage(Role.USER, "no")])


def test_remembers_that_it_was_closed() -> None:
    provider = ScriptedModelProvider([])
    assert not provider.closed
    asyncio.run(provider.aclose())
    assert provider.closed
