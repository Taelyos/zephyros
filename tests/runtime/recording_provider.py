"""A model provider that answers what the obedient stub answers, and records whether each call
carried the base instruction of the instances (ADR 0034).

The runtime tests run it in the hub container, behind the gateway, as the `openai-compatible`
provider of the configuration: `python -c <this file> PORT KEY MARKER`. A call that offers tools
is a call of an agent at work; it tells which engine made it by the names of the hub's tools it
offers (`mcp__zephyros__…` for Hermes, `zephyros_…` for OpenCode). `GET /stats` tells, by
engine, how many such calls came, and how many had MARKER in a system message.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from zephyros.adapters.dummy.obedient_stub import ObedientStubProvider
from zephyros.core.gateway import GatewayError, parse_chat_request, render_chat_response

STUB = ObedientStubProvider()
LOCK = threading.Lock()
SEEN: dict[str, dict[str, int]] = {}


def engine_of(tools: list[Any]) -> str:
    names = [
        str(tool.get("function", {}).get("name", "")) for tool in tools if isinstance(tool, dict)
    ]
    if any(name.startswith("mcp__zephyros__") for name in names):
        return "hermes"
    if any(name.startswith("zephyros_") for name in names):
        return "opencode"
    return "other"


def instructed(messages: list[Any], marker: str) -> bool:
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        content = message.get("content")
        text = content if isinstance(content, str) else json.dumps(content)
        if marker in text:
            return True
    return False


class Handler(BaseHTTPRequestHandler):
    key = ""
    marker = ""

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
            with LOCK:
                self.reply(200, SEEN)
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
        tools = body.get("tools") or []
        if tools:
            engine = engine_of(tools)
            with LOCK:
                counts = SEEN.setdefault(engine, {"calls": 0, "instructed": 0})
                counts["calls"] += 1
                counts["instructed"] += instructed(body.get("messages") or [], self.marker)
        try:
            request = parse_chat_request(body, model=str(body.get("model")))
        except GatewayError as error:
            self.reply(400, {"error": {"message": str(error)}})
            return
        response = asyncio.run(STUB.complete(request))
        self.reply(200, render_chat_response(response, created=int(time.time()), identifier="c"))


def main(port: int, key: str, marker: str) -> None:
    Handler.key = key
    Handler.marker = marker
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main(int(sys.argv[1]), sys.argv[2], sys.argv[3])
