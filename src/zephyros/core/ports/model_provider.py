"""The model provider interface: how the hub obtains an answer to a model call.

A model provider answers a chat conversation, possibly with calls to tools that the
caller offered. The hub's model gateway translates the wire format of an instance into
these types and back. The provider never sees a project token.

Version 0.x. No streaming: a provider answers whole, and the gateway streams a whole answer to
a client that asks for a stream.

Version 0.2: an assistant message can carry the reasoning of the model (`reasoning`), the text a
thinking model gives before its answer, which some providers want back with the conversation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

INTERFACE_VERSION = "0.2.0"


class ModelProviderError(Exception):
    """The provider refused or could not answer the request."""


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A call to a tool, asked for by the model. `arguments` is the JSON text of an object."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool the caller offers. `parameters` is a JSON Schema."""

    name: str
    description: str
    parameters: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Role
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    reasoning: str | None = None
    """What a thinking model reasoned before this answer, when it says (assistant only)."""

    def __post_init__(self) -> None:
        if self.role is not Role.ASSISTANT and self.tool_calls:
            raise ValueError("only an assistant message can carry tool calls")
        if self.role is not Role.ASSISTANT and self.reasoning is not None:
            raise ValueError("only an assistant message carries the reasoning of the model")
        if self.role is Role.TOOL:
            if not self.tool_call_id or self.content is None:
                raise ValueError("a tool message needs a tool call identifier and a content")
        elif self.tool_call_id is not None:
            raise ValueError("only a tool message answers a tool call")
        if self.role is Role.ASSISTANT:
            if self.content is None and not self.tool_calls:
                raise ValueError("an assistant message needs a content or tool calls")
        elif self.content is None:
            raise ValueError(f"a {self.role.value} message needs a content")


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int

    def __post_init__(self) -> None:
        if self.prompt_tokens < 0 or self.completion_tokens < 0:
            raise ValueError("token counts cannot be negative")

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class ModelRequest:
    model: str
    messages: tuple[ChatMessage, ...]
    tools: tuple[ToolSpec, ...] = ()

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("model must not be empty")


@dataclass(frozen=True, slots=True)
class ModelResponse:
    model: str
    message: ChatMessage
    usage: Usage

    def __post_init__(self) -> None:
        if self.message.role is not Role.ASSISTANT:
            raise ValueError("the answer of a model is an assistant message")


class ModelProvider(Protocol):
    """Answers chat conversations."""

    @property
    def interface_version(self) -> str:
        """The version of this interface that the implementation follows."""
        ...

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Answer the conversation, or raise ModelProviderError.

        An empty conversation is refused. A tool call in the answer names a tool that the
        request offered.
        """
        ...

    async def aclose(self) -> None:
        """Release what the provider holds (connections, files). Called once, in the loop."""
        ...
