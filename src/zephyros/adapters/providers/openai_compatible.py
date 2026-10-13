"""A model provider that forwards to an OpenAI-compatible service.

The credential is read from a file at each call, so that changing it in the hub takes effect on
the next call (US-E3-01). It is sent to the configured upstream and nowhere else, and it never
appears in an error: a failure of the upstream is reported by its status code alone.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx

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

TIMEOUT_SECONDS = 120.0


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


def _wire_message(message: ChatMessage) -> dict[str, object]:
    wire: dict[str, object] = {"role": message.role.value, "content": message.content}
    if message.tool_calls:
        wire["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
            for c in message.tool_calls
        ]
    if message.tool_call_id is not None:
        wire["tool_call_id"] = message.tool_call_id
    return wire


class OpenAiCompatibleProvider:
    def __init__(
        self,
        base_url: str,
        credential: FileCredential,
        *,
        upstream_model: str,
        session_header: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._credential = credential
        self._upstream_model = upstream_model
        self._session_header = session_header
        # One session name per process: the providers that route by session (OpenCode Go asks
        # for `x-opencode-session`) keep this hub's traffic together.
        self._session = f"zephyros-{uuid.uuid4().hex}"
        self._client = client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False)

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
            "messages": [_wire_message(m) for m in request.messages],
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
            raise ModelProviderError(f"the provider answered HTTP {response.status_code}")
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
            reply = ChatMessage(Role.ASSISTANT, message.get("content"), calls)
            tokens = Usage(int(usage["prompt_tokens"]), int(usage["completion_tokens"]))
        except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError):
            raise ModelProviderError("the provider's answer is not a chat completion") from None
        offered = {tool.name for tool in request.tools}
        if any(call.name not in offered for call in calls):
            raise ModelProviderError("the provider called a tool that was not offered")
        return ModelResponse(request.model, reply, tokens)

    async def aclose(self) -> None:
        await self._client.aclose()
