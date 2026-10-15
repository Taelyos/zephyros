"""Types of the model provider interface."""

from __future__ import annotations

import pytest

from zephyros.core.ports.model_provider import (
    ChatMessage,
    ModelRequest,
    ModelResponse,
    Role,
    ToolCall,
    Usage,
)

CALL = ToolCall("c1", "echo", "{}")


class TestChatMessage:
    def test_accepts_the_normal_messages(self) -> None:
        ChatMessage(Role.SYSTEM, "s")
        ChatMessage(Role.USER, "u")
        ChatMessage(Role.ASSISTANT, "a")
        ChatMessage(Role.ASSISTANT, None, (CALL,))
        ChatMessage(Role.ASSISTANT, "a", (CALL,))
        ChatMessage(Role.TOOL, "result", tool_call_id="c1")
        ChatMessage(Role.ASSISTANT, None, (CALL,), reasoning="r")

    @pytest.mark.parametrize("role", [Role.SYSTEM, Role.USER])
    def test_only_an_assistant_message_carries_reasoning(self, role: Role) -> None:
        with pytest.raises(ValueError, match="reasoning"):
            ChatMessage(role, "x", reasoning="r")
        with pytest.raises(ValueError, match="reasoning"):
            ChatMessage(Role.TOOL, "x", tool_call_id="c1", reasoning="r")

    @pytest.mark.parametrize(
        ("role", "content", "calls", "call_id"),
        [
            (Role.USER, "u", (CALL,), None),
            (Role.SYSTEM, "s", (CALL,), None),
            (Role.TOOL, "r", (), None),
            (Role.TOOL, None, (), "c1"),
            (Role.USER, "u", (), "c1"),
            (Role.ASSISTANT, None, (), None),
            (Role.USER, None, (), None),
        ],
    )
    def test_refuses_the_impossible_messages(
        self, role: Role, content: str | None, calls: tuple[ToolCall, ...], call_id: str | None
    ) -> None:
        with pytest.raises(ValueError, match=r"."):
            ChatMessage(role, content, calls, call_id)


class TestUsage:
    def test_adds_up(self) -> None:
        assert Usage(3, 4).total_tokens == 7

    def test_refuses_negative_counts(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            Usage(-1, 0)
        with pytest.raises(ValueError, match="negative"):
            Usage(0, -1)


class TestRequestAndResponse:
    def test_a_request_names_a_model(self) -> None:
        with pytest.raises(ValueError, match="model"):
            ModelRequest(model="", messages=())

    def test_an_answer_comes_from_the_assistant(self) -> None:
        with pytest.raises(ValueError, match="assistant"):
            ModelResponse("m", ChatMessage(Role.USER, "u"), Usage(1, 1))
        ModelResponse("m", ChatMessage(Role.ASSISTANT, "a"), Usage(1, 1))
