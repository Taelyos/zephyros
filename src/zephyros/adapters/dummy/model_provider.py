"""A scripted model provider: answers with replies written in advance."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from zephyros.core.ports.model_provider import (
    INTERFACE_VERSION,
    ChatMessage,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    Role,
    Usage,
)


def _words(messages: Iterable[ChatMessage]) -> int:
    count = 0
    for message in messages:
        count += len((message.content or "").split())
        count += sum(1 + len(call.arguments.split()) for call in message.tool_calls)
    return count


class ScriptedModelProvider:
    """Replays its replies in order. It is the seed of the simulated model provider.

    It records every request it receives in `requests`. It refuses an empty conversation, a
    request after the last reply, and a reply that calls a tool the request did not offer.
    """

    def __init__(self, replies: Iterable[ChatMessage]) -> None:
        self._replies = deque(replies)
        for reply in self._replies:
            if reply.role is not Role.ASSISTANT:
                raise ValueError("a scripted reply is an assistant message")
        self.requests: list[ModelRequest] = []
        self.closed = False

    @property
    def interface_version(self) -> str:
        return INTERFACE_VERSION

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not request.messages:
            raise ModelProviderError("the conversation is empty")
        if not self._replies:
            raise ModelProviderError("the script has no reply left")
        offered = {tool.name for tool in request.tools}
        for call in self._replies[0].tool_calls:
            if call.name not in offered:
                raise ModelProviderError(
                    f"the scripted reply calls a tool not offered: {call.name}"
                )
        reply = self._replies.popleft()
        self.requests.append(request)
        usage = Usage(prompt_tokens=_words(request.messages), completion_tokens=_words([reply]))
        return ModelResponse(model=request.model, message=reply, usage=usage)

    async def aclose(self) -> None:
        self.closed = True
