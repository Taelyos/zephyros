"""A model provider that forwards to an OpenAI-compatible service.

The credential is read from a file at each call, so that changing it in the hub takes effect on
the next call (US-E3-01). It is sent to the configured upstream and nowhere else, and it never
appears in an error: a failure of the upstream is reported by its status code alone.

The reasoning of a thinking model (`reasoning_content`) is read from every answer. Whether it
goes back with the conversation is the choice of the configuration (`gateway.reasoning_echo`,
ADR 0032), because providers disagree: DeepSeek in thinking mode refuses an assistant message
without it (HTTP 400), and strict OpenAI-compatible providers refuse one with it. When it goes
back, every assistant message carries it, and one that has none (an answer without reasoning,
or an engine that dropped it) carries a single space, which DeepSeek accepts and an empty text
is not.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx

from zephyros.core.masking import mask_text
from zephyros.core.ports.model_provider import (
    INTERFACE_VERSION,
    ChatMessage,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    Role,
    ToolCall,
    Usage,
)

TIMEOUT_SECONDS = 600.0
REASON_CHARACTERS = 300
"""How much of the provider's reason for refusing a request (HTTP 400) the error carries."""
REASONING_PAD = " "
"""The reasoning of an assistant message that has none, when the provider wants it back."""


class FileCredential:
    """The model credential, read from a file at each use."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> str:
        try:
            value = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        return value

    def secrets(self) -> list[str]:
        value = self.read()
        return [value] if value else []


def _wire_message(message: ChatMessage, *, reasoning_echo: bool) -> dict[str, object]:
    wire: dict[str, object] = {"role": message.role.value, "content": message.content}
    if reasoning_echo and message.role is Role.ASSISTANT:
        wire["reasoning_content"] = message.reasoning or REASONING_PAD
    if message.tool_calls:
        wire["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
            for c in message.tool_calls
        ]
    if message.tool_call_id is not None:
        wire["tool_call_id"] = message.tool_call_id
    return wire


def _refusal_reason(response: httpx.Response, credential: str) -> str:
    """For a request the provider refuses as malformed (HTTP 400), the `error.message` of its
    JSON answer, masked and bounded: without it, the third real run could not tell why a turn
    failed. Every other status is reported alone — an authentication error may echo a key."""
    if response.status_code != 400:
        return ""
    try:
        error = response.json().get("error")
        message = error.get("message") if isinstance(error, dict) else None
    except (ValueError, AttributeError):
        return ""
    if not isinstance(message, str) or not message.strip():
        return ""
    text = mask_text(" ".join(message.split()), [credential])[:REASON_CHARACTERS]
    return f": {text}"


class OpenAiCompatibleProvider:
    def __init__(
        self,
        base_url: str,
        credential: FileCredential,
        *,
        upstream_model: str,
        session_header: str | None = None,
        reasoning_echo: bool = False,
        timeout_seconds: float = TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._credential = credential
        self._upstream_model = upstream_model
        self._session_header = session_header
        self._reasoning_echo = reasoning_echo
        # One session name per process: the providers that route by session (OpenCode Go asks
        # for `x-opencode-session`) keep this hub's traffic together.
        self._session = f"zephyros-{uuid.uuid4().hex}"
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)

    @property
    def interface_version(self) -> str:
        return INTERFACE_VERSION

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not request.messages:
            raise ModelProviderError("the conversation is empty")
        credential = self._credential.read()
        if not credential:
            raise ModelProviderError("no model credential is configured")
        body: dict[str, object] = {
            "model": self._upstream_model,
            "messages": [
                _wire_message(m, reasoning_echo=self._reasoning_echo) for m in request.messages
            ],
        }
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": dict(t.parameters),
                    },
                }
                for t in request.tools
            ]
        headers = {"Authorization": f"Bearer {credential}"}
        if self._session_header:
            headers[self._session_header] = self._session
        try:
            response = await self._client.post(self._url, json=body, headers=headers)
        except httpx.HTTPError as error:
            raise ModelProviderError(
                f"the provider could not be reached ({type(error).__name__})"
            ) from None
        if response.status_code != 200:
            raise ModelProviderError(
                f"the provider answered HTTP {response.status_code}"
                + _refusal_reason(response, credential)
            )
        return self._parse(request, response)

    @staticmethod
    def _parse(request: ModelRequest, response: httpx.Response) -> ModelResponse:
        try:
            data = response.json()
            message = data["choices"][0]["message"]
            usage = data["usage"]
            calls = tuple(
                ToolCall(str(c["id"]), str(c["function"]["name"]), str(c["function"]["arguments"]))
                for c in message.get("tool_calls") or []
            )
            reasoning = message.get("reasoning_content")
            reply = ChatMessage(
                Role.ASSISTANT,
                message.get("content"),
                calls,
                reasoning=reasoning if isinstance(reasoning, str) and reasoning else None,
            )
            tokens = Usage(int(usage["prompt_tokens"]), int(usage["completion_tokens"]))
        except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError):
            raise ModelProviderError("the provider's answer is not a chat completion") from None
        offered = {tool.name for tool in request.tools}
        if any(call.name not in offered for call in calls):
            raise ModelProviderError("the provider called a tool that was not offered")
        return ModelResponse(request.model, reply, tokens)

    async def aclose(self) -> None:
        await self._client.aclose()
