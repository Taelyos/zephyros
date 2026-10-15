"""The model gateway of the hub (EXG-04; US-E3-01; ADR 0008).

An instance sends a chat conversation in the OpenAI wire format with its project token. The
gateway checks the token's rights, translates the conversation into the types of the
`ModelProvider` interface, asks the provider, and translates the answer back. The reasoning of a
thinking model (`reasoning_content`) goes both ways: to the instance with the answer, and back
to the provider with the conversation, where the provider decides what to send (ADR 0032). The model
credential lives behind the provider and nowhere else: it is never part of a request, an
answer, an error or a log record. Every call leaves one record naming the instance, the model
and the tokens used.

This is a minimal gateway. Presets, budgets and several providers are later work.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from zephyros.core.audit import Action, AuditEvent, AuditLog, Outcome, Reason
from zephyros.core.hub import Caller
from zephyros.core.ports.model_provider import (
    ChatMessage,
    ModelProvider,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    Role,
    ToolCall,
    ToolSpec,
)
from zephyros.core.rights import decide

MAX_MESSAGES = 1000
MAX_TOOLS = 256
MAX_CHARACTERS = 1_000_000
REDACTED = "[redacted]"


class CredentialSource(Protocol):
    """Where the model credential is read from, at each call, so that a change takes effect."""

    def secrets(self) -> list[str]:
        """The credential(s) currently in force, for redaction. Never logged."""
        ...


class GatewayError(Exception):
    """A call the gateway refuses or cannot complete, with the answer to give."""

    def __init__(
        self, status: int, code: str, message: str, reason: Reason = Reason.INVALID_REQUEST
    ) -> None:
        self.status = status
        self.code = code
        self.reason = reason
        super().__init__(message)

    def body(self) -> dict[str, object]:
        return {"error": {"message": str(self), "type": "invalid_request_error", "code": self.code}}


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace every occurrence of a secret in `text`."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


# ----------------------------------------------------------------------------- the wire format
def _text_of(content: object, where: str) -> str | None:
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if (
                isinstance(part, Mapping)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            ):
                parts.append(str(part["text"]))
            else:
                raise GatewayError(
                    400, "unsupported_content", f"{where}: only text content is supported"
                )
        return "".join(parts)
    raise GatewayError(
        400, "invalid_content", f"{where}: the content is neither text nor a list of text parts"
    )


def _tool_call(raw: object, where: str) -> ToolCall:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("function"), Mapping):
        raise GatewayError(
            400, "invalid_tool_call", f"{where}: a tool call is {{id, type, function}}"
        )
    function = raw["function"]
    arguments = function.get("arguments", "{}")
    if isinstance(arguments, Mapping):
        arguments = json.dumps(arguments)
    identifier, name = raw.get("id"), function.get("name")
    if (
        not isinstance(identifier, str)
        or not isinstance(name, str)
        or not isinstance(arguments, str)
    ):
        raise GatewayError(
            400, "invalid_tool_call", f"{where}: a tool call has an id, a name and arguments"
        )
    return ToolCall(identifier, name, arguments)


def _reasoning_of(raw: Mapping[str, object], where: str) -> str | None:
    """The reasoning an assistant message carries back, if any. Another type is refused rather
    than dropped: the provider that asks for the field would refuse the call for it anyway."""
    reasoning = raw.get("reasoning_content")
    if reasoning is None or isinstance(reasoning, str):
        return reasoning
    raise GatewayError(400, "invalid_message", f"{where}: reasoning_content is a text")


_ROLES = {
    "system": Role.SYSTEM,
    "developer": Role.SYSTEM,
    "user": Role.USER,
    "assistant": Role.ASSISTANT,
    "tool": Role.TOOL,
}


def _message(raw: object, index: int) -> ChatMessage:
    where = f"messages[{index}]"
    if not isinstance(raw, Mapping):
        raise GatewayError(400, "invalid_message", f"{where} is not an object")
    role = _ROLES.get(str(raw.get("role")))
    if role is None:
        raise GatewayError(400, "invalid_message", f"{where}: unknown role")
    content = _text_of(raw.get("content"), where)
    calls = tuple(_tool_call(call, where) for call in raw.get("tool_calls") or [])
    try:
        if role is Role.TOOL:
            return ChatMessage(role, content, tool_call_id=str(raw.get("tool_call_id") or ""))
        if role is Role.ASSISTANT:
            return ChatMessage(role, content, calls, reasoning=_reasoning_of(raw, where))
        return ChatMessage(role, content)
    except ValueError as error:
        raise GatewayError(400, "invalid_message", f"{where}: {error}") from error


def parse_chat_request(body: object, *, model: str) -> ModelRequest:
    """The conversation of an OpenAI chat request. Unknown top-level fields are ignored; the
    structure of messages and tools is not."""
    if not isinstance(body, Mapping):
        raise GatewayError(400, "invalid_request", "the body is not an object")
    if body.get("model") != model:
        raise GatewayError(
            404, "model_not_found", "this gateway serves one model; ask for it by its name"
        )
    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise GatewayError(400, "invalid_request", "messages is a non-empty list")
    if len(raw_messages) > MAX_MESSAGES:
        raise GatewayError(400, "too_many_messages", f"at most {MAX_MESSAGES} messages")
    messages = tuple(_message(raw, index) for index, raw in enumerate(raw_messages))
    size = sum(
        len(m.content or "") + len(m.reasoning or "") + sum(len(c.arguments) for c in m.tool_calls)
        for m in messages
    )
    if size > MAX_CHARACTERS:
        raise GatewayError(413, "too_large", "the conversation is too large")
    raw_tools = body.get("tools") or []
    if not isinstance(raw_tools, list) or len(raw_tools) > MAX_TOOLS:
        raise GatewayError(
            400, "invalid_tools", f"tools is a list of at most {MAX_TOOLS} functions"
        )
    tools: list[ToolSpec] = []
    for index, raw in enumerate(raw_tools):
        function = raw.get("function") if isinstance(raw, Mapping) else None
        if not isinstance(function, Mapping) or not isinstance(function.get("name"), str):
            raise GatewayError(
                400, "invalid_tools", f"tools[{index}] is {{type: function, function: {{name}}}}"
            )
        parameters = function.get("parameters") or {}
        if not isinstance(parameters, Mapping):
            raise GatewayError(
                400, "invalid_tools", f"tools[{index}]: parameters is a JSON Schema object"
            )
        tools.append(
            ToolSpec(
                str(function["name"]), str(function.get("description") or ""), dict(parameters)
            )
        )
    return ModelRequest(model=model, messages=messages, tools=tuple(tools))


def render_chat_response(
    response: ModelResponse, *, created: int, identifier: str
) -> dict[str, Any]:
    message = response.message
    rendered: dict[str, Any] = {"role": "assistant", "content": message.content}
    if message.reasoning:
        rendered["reasoning_content"] = message.reasoning
    if message.tool_calls:
        rendered["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
    return {
        "id": identifier,
        "object": "chat.completion",
        "created": created,
        "model": response.model,
        "choices": [
            {
                "index": 0,
                "message": rendered,
                "finish_reason": "tool_calls" if message.tool_calls else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens,
        },
    }


def render_stream(completion: Mapping[str, Any], *, include_usage: bool) -> list[str]:
    """An answer already complete, as the events of a streamed one (one delta, then the end)."""
    choice = completion["choices"][0]
    base = {
        "id": completion["id"],
        "object": "chat.completion.chunk",
        "created": completion["created"],
        "model": completion["model"],
    }

    def event(delta: Mapping[str, Any], finish: str | None = None) -> str:
        payload = {**base, "choices": [{"index": 0, "delta": dict(delta), "finish_reason": finish}]}
        return f"data: {json.dumps(payload)}\n\n"

    message = choice["message"]
    events = [event({"role": "assistant", "content": ""})]
    if message.get("reasoning_content"):
        events.append(event({"reasoning_content": message["reasoning_content"]}))
    if message.get("tool_calls"):
        events.append(
            event(
                {
                    "tool_calls": [
                        {"index": i, **call} for i, call in enumerate(message["tool_calls"])
                    ]
                }
            )
        )
    elif message.get("content"):
        events.append(event({"content": message["content"]}))
    events.append(event({}, choice["finish_reason"]))
    if include_usage:
        events.append(
            f"data: {json.dumps({**base, 'choices': [], 'usage': completion['usage']})}\n\n"
        )
    events.append("data: [DONE]\n\n")
    return events


# ----------------------------------------------------------------------------- the service
@dataclass(frozen=True, slots=True)
class GatewayAnswer:
    completion: dict[str, Any]
    stream: bool
    include_usage: bool


class GatewayService:
    def __init__(
        self,
        *,
        audit: AuditLog,
        provider: ModelProvider,
        model: str,
        credentials: CredentialSource | None = None,
    ) -> None:
        self._audit = audit
        self._provider = provider
        self._model = model
        self._credentials = credentials

    def _secrets(self) -> list[str]:
        return self._credentials.secrets() if self._credentials is not None else []

    def _log(
        self, caller: Caller, outcome: Outcome, reason: Reason, parameters: Mapping[str, object]
    ) -> None:
        stream = caller.principal.project_id or "hub"
        self._audit.append(
            AuditEvent(
                stream,
                caller.principal.id,
                Action.GATEWAY_CALL,
                outcome,
                reason,
                {"source": caller.source, **parameters},
            )
        )

    async def complete(self, caller: Caller, body: object) -> GatewayAnswer:
        """Answer one chat call, or raise GatewayError. Exactly one record is written."""
        decision = decide(caller.principal, Action.GATEWAY_CALL, caller.principal.project_id)
        if not decision.allowed:
            self._log(caller, Outcome.REFUSED, decision.reason, {})
            raise GatewayError(
                403, "forbidden", "this caller has no access to the model gateway", decision.reason
            )
        try:
            request = parse_chat_request(body, model=self._model)
        except GatewayError as error:
            self._log(
                caller, Outcome.REFUSED, error.reason, {"detail": str(error), "code": error.code}
            )
            raise
        try:
            response = await self._provider.complete(request)
        except ModelProviderError as error:
            detail = redact(str(error), self._secrets())[:200]
            self._log(caller, Outcome.ERROR, Reason.NONE, {"model": self._model, "detail": detail})
            raise GatewayError(502, "upstream_failure", "the model provider failed") from None
        self._log(
            caller,
            Outcome.OK,
            Reason.NONE,
            {
                "model": response.model,
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "tool_calls": len(response.message.tool_calls),
            },
        )
        completion = render_chat_response(
            response, created=int(time.time()), identifier="chatcmpl-" + uuid.uuid4().hex[:24]
        )
        text = json.dumps(completion)
        if any(secret and secret in text for secret in self._secrets()):
            completion = json.loads(redact(text, self._secrets()))
        options = body.get("stream_options") if isinstance(body, Mapping) else None
        include_usage = isinstance(options, Mapping) and options.get("include_usage") is True
        return GatewayAnswer(
            completion,
            stream=isinstance(body, Mapping) and body.get("stream") is True,
            include_usage=include_usage,
        )
