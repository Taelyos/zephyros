"""A model provider that wants its reasoning back, as DeepSeek in thinking mode does (ADR 0032).

The runtime tests run it in the hub container, behind the gateway, as the `openai-compatible`
provider of the configuration: `python -c <this file> PORT KEY`. It answers what the obedient
stub answers (ADR 0002), so a turn does what its prompt says, and it holds the gateway and the
engines to what a thinking model demands:

- a conversation in which an assistant message with tool calls carries no `reasoning_content`
  (or an empty one) is refused, HTTP 400, with DeepSeek's words;
- each answer gives a reasoning of its own, `reasoning-<n>`, save the first answer with tool
  calls of a conversation: two tool calls at once, and no reasoning, the answer after which a
  real run was refused.

`GET /stats` tells what it saw: the calls, the refusals, the reasonings it gave, those that came
back, and the assistant messages that came back with a pad (a text of spaces) instead.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from zephyros.adapters.dummy.obedient_stub import ObedientStubProvider
from zephyros.core.gateway import GatewayError, parse_chat_request, render_chat_response
from zephyros.core.ports.model_provider import (
    ChatMessage,
    ModelRequest,
    ModelResponse,
    Role,
)

REFUSAL = "The `reasoning_content` in the thinking mode must be passed back to the API."


class Seen:
    """What the provider saw, under one lock."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls = 0
        self.refused = 0
        self.given: list[str] = []
        self.back: set[str] = set()
        self.padded = 0
        self.without_reasoning = 0

    def stats(self) -> dict[str, Any]:
        with self.lock:
            return {
                "calls": self.calls,
                "refused": self.refused,
                "given": list(self.given),
                "back": sorted(self.back),
                "padded": self.padded,
                "without_reasoning": self.without_reasoning,
            }


SEEN = Seen()
STUB = ObedientStubProvider()


def missing_reasoning(messages: list[Any]) -> bool:
    """Whether an assistant message with tool calls comes without its reasoning, and records
    what came back."""
    missing = False
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        reasoning = message.get("reasoning_content")
        if not isinstance(reasoning, str) or not reasoning:
            missing = missing or bool(message.get("tool_calls"))
            continue
        with SEEN.lock:
            if reasoning in SEEN.given:
                SEEN.back.add(reasoning)
            elif not reasoning.strip():
                SEEN.padded += 1
    return missing


def answer(request: ModelRequest) -> ModelResponse:
    first = asyncio.run(STUB.complete(request))
    calls = first.message.tool_calls
    if calls and not any(m.tool_calls for m in request.messages):
        # The first answer with tool calls: the next instruction too, in the same answer,
        # and no reasoning.
        ahead = ModelRequest(
            request.model,
            (
                *request.messages,
                first.message,
                *(ChatMessage(Role.TOOL, "pending", tool_call_id=c.id) for c in calls),
            ),
            request.tools,
        )
        second = asyncio.run(STUB.complete(ahead)).message.tool_calls
        with SEEN.lock:
            SEEN.without_reasoning += 1
        reply = ChatMessage(Role.ASSISTANT, None, calls + second)
        return ModelResponse(request.model, reply, first.usage)
    with SEEN.lock:
        reasoning = f"reasoning-{len(SEEN.given) + 1}"
        SEEN.given.append(reasoning)
    return replace(first, message=replace(first.message, reasoning=reasoning))


class Handler(BaseHTTPRequestHandler):
    key = ""

    def log_message(self, *args: object) -> None:
        pass

    def reply(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/stats":
            self.reply(200, SEEN.stats())
        else:
            self.reply(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        if not self.path.endswith("/chat/completions"):
            self.reply(404, {"error": {"message": "not found"}})
            return
        if self.headers.get("authorization") != f"Bearer {self.key}":
            self.reply(401, {"error": {"message": "no such key"}})
            return
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        with SEEN.lock:
            SEEN.calls += 1
        if missing_reasoning(body.get("messages") or []):
            with SEEN.lock:
                SEEN.refused += 1
            self.reply(400, {"error": {"message": REFUSAL, "type": "invalid_request_error"}})
            return
        try:
            request = parse_chat_request(body, model=str(body.get("model")))
        except GatewayError as error:
            self.reply(400, {"error": {"message": str(error)}})
            return
        response = answer(request)
        self.reply(200, render_chat_response(response, created=int(time.time()), identifier="c"))


def main(port: int, key: str) -> None:
    Handler.key = key
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main(int(sys.argv[1]), sys.argv[2])
