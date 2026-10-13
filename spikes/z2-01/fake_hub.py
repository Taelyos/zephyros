"""Fake hub for spike Z2-01: the model endpoint and the MCP endpoint on ONE address.

The contract allows an execution context exactly one hub destination (annex "Hub endpoints
open to instances"), so the OpenAI-compatible endpoint (/v1) and the MCP endpoint (/mcp) are
served by the same server on the same port. Everything the instance sends is recorded, one
JSON object per line on stdout, with the bearer reduced to ok / wrong / missing.

Environment:
    FAKE_TOKEN      the bearer every request must carry (default: a fixed test value)
    FAKE_TOKEN_NEW  optional second accepted bearer, to test a token change
    FAKE_SCENARIO   path of a JSON list of steps for the model, see script_step()
    FAKE_PORT       listening port (default 8080)
    FAKE_MCP_SSE    "1" answers MCP POSTs as text/event-stream instead of JSON
    FAKE_DELAY      seconds to hold each model call that carries tools (time to sample)
    FAKE_MCP_DOWN   "1" answers every MCP request with 503 (the MCP half of the hub is down)

Throwaway code for a spike. Nothing in src/ imports it.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TOKEN = os.environ.get("FAKE_TOKEN", "zph_c_spike-test-token-0000000000000000")
TOKEN_NEW = os.environ.get("FAKE_TOKEN_NEW", "")
PORT = int(os.environ.get("FAKE_PORT", "8080"))
MCP_SSE = os.environ.get("FAKE_MCP_SSE") == "1"
DELAY = float(os.environ.get("FAKE_DELAY", "0"))  # seconds a main model call is held
MCP_DOWN = os.environ.get("FAKE_MCP_DOWN") == "1"  # answer every MCP request with 503
SCENARIO_PATH = os.environ.get("FAKE_SCENARIO", "")
MODEL_ID = "fake-model"

_lock = threading.Lock()
_main_calls = 0  # chat requests that carried tools: the steps of the script


def emit(record: dict[str, Any]) -> None:
    record["t"] = round(time.time(), 3)
    with _lock:
        print(json.dumps(record, sort_keys=True), flush=True)


def load_script() -> list[dict[str, Any]]:
    if not SCENARIO_PATH:
        return [{"text": "OK from the fake model."}]
    with open(SCENARIO_PATH, encoding="utf-8") as handle:
        steps: list[dict[str, Any]] = json.load(handle)
    return steps


def script_step(index: int, tool_names: list[str]) -> dict[str, Any]:
    """Step ``index`` of the scenario as a message.

    A step is {"text": "..."} or {"tool": "name"} / {"tool_like": "suffix"} with "args".
    ``tool_like`` picks the first advertised tool whose name ends with the suffix, because
    the exact name Hermes gives an MCP tool is one of the things the spike measures.
    """
    steps = load_script()
    step = steps[min(index, len(steps) - 1)]
    if "text" in step:
        return {"role": "assistant", "content": step["text"]}
    name = step.get("tool", "")
    if not name:
        suffix = step["tool_like"]
        matches = [n for n in tool_names if n.endswith(suffix)]
        name = matches[0] if matches else f"__missing__{suffix}"
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(step.get("args", {}))},
            }
        ],
    }


def tool_names_of(request: dict[str, Any]) -> list[str]:
    names = []
    for tool in request.get("tools") or []:
        function = tool.get("function", tool)
        names.append(function.get("name", "?"))
    return names


def last_tool_result(request: dict[str, Any]) -> str | None:
    """Start of the content of the last tool message, to see what a tool returned."""
    messages = request.get("messages") or []
    if messages and messages[-1].get("role") == "tool":
        content = messages[-1].get("content")
        return str(content)[:600]
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: ARG002 - silences the default log
        return

    # ---- helpers
    def auth_state(self) -> str:
        header = self.headers.get("Authorization", "")
        if not header:
            return "missing"
        accepted = {f"Bearer {TOKEN}"}
        if TOKEN_NEW:
            accepted.add(f"Bearer {TOKEN_NEW}")
        if header in accepted:
            return "new" if TOKEN_NEW and header == f"Bearer {TOKEN_NEW}" else "ok"
        return "wrong"

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def send_json(self, status: int, payload: Any, extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def record(self, kind: str, auth: str, **fields: Any) -> None:
        emit(
            {
                "kind": kind,
                "method": self.command,
                "path": self.path,
                "auth": auth,
                "client": self.client_address[0],
                "user_agent": self.headers.get("User-Agent", ""),
                "header_names": sorted(self.headers.keys()),
                **fields,
            }
        )

    # ---- verbs
    def do_GET(self) -> None:
        auth = self.auth_state()
        if self.path.startswith("/v1/models"):
            self.record("models", auth)
            if auth not in ("ok", "new"):
                return self.send_json(401, {"error": {"message": "bad token"}})
            return self.send_json(
                200, {"object": "list", "data": [{"id": MODEL_ID, "object": "model"}]}
            )
        if self.path.startswith("/mcp"):
            self.record("mcp-get", auth)
            return self.send_json(405, {"error": "no server-initiated stream"})
        self.record("other", auth)
        self.send_json(404, {"error": "unknown path"})

    def do_DELETE(self) -> None:
        self.record("mcp-delete" if self.path.startswith("/mcp") else "other", self.auth_state())
        self.send_json(200, {})

    def do_POST(self) -> None:
        auth = self.auth_state()
        raw = self.read_body()
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}
        if self.path.startswith("/v1/chat/completions"):
            return self.chat(auth, body)
        if self.path.startswith("/mcp"):
            return self.mcp(auth, body)
        self.record("other", auth, size=len(raw))
        self.send_json(404, {"error": "unknown path"})

    # ---- model endpoint
    def chat(self, auth: str, body: dict[str, Any]) -> None:
        global _main_calls
        names = tool_names_of(body)
        roles = [m.get("role") for m in body.get("messages", [])]
        streaming = bool(body.get("stream"))
        self.record(
            "chat",
            auth,
            model=body.get("model"),
            stream=streaming,
            n_messages=len(roles),
            last_roles=roles[-3:],
            n_tools=len(names),
            tool_names=names if names and _main_calls == 0 else None,
            stream_options=body.get("stream_options"),
            last_tool_result=last_tool_result(body),
        )
        if auth not in ("ok", "new"):
            return self.send_json(401, {"error": {"message": "bad token"}})
        if names:
            with _lock:
                index = _main_calls
                _main_calls += 1
            message = script_step(index, names)
            if DELAY:
                time.sleep(DELAY)
        else:  # auxiliary call (title, summary): answer plainly
            message = {"role": "assistant", "content": "ok"}
        usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        if not streaming:
            return self.send_json(
                200,
                {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:10]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": body.get("model", MODEL_ID),
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": usage,
                },
            )
        self.stream_chat(body, message, finish, usage)

    def stream_chat(
        self, body: dict[str, Any], message: dict[str, Any], finish: str, usage: dict[str, int]
    ) -> None:
        cid = f"chatcmpl-{uuid.uuid4().hex[:10]}"
        model = body.get("model", MODEL_ID)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def chunk(
            delta: dict[str, Any], reason: str | None = None, with_usage: bool = False
        ) -> None:
            payload: dict[str, Any] = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]
                if not with_usage
                else [],
            }
            if with_usage:
                payload["usage"] = usage
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
            self.wfile.flush()

        chunk({"role": "assistant", "content": ""})
        if message.get("tool_calls"):
            call = message["tool_calls"][0]
            fn = call["function"]
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": call["id"],
                            "type": "function",
                            "function": {"name": fn["name"], "arguments": ""},
                        }
                    ]
                }
            )
            args = fn["arguments"]
            half = max(1, len(args) // 2)
            for piece in (args[:half], args[half:]):
                chunk({"tool_calls": [{"index": 0, "function": {"arguments": piece}}]})
        else:
            text = message.get("content") or ""
            for piece in (text[: len(text) // 2], text[len(text) // 2 :]):
                if piece:
                    chunk({"content": piece})
        chunk({}, reason=finish)
        chunk({}, with_usage=True)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    # ---- MCP endpoint (streamable HTTP, JSON-RPC)
    def mcp(self, auth: str, rpc: dict[str, Any]) -> None:
        method = rpc.get("method")
        self.record(
            "mcp",
            auth,
            rpc_method=method,
            session=self.headers.get("Mcp-Session-Id"),
            protocol=self.headers.get("MCP-Protocol-Version"),
        )
        if MCP_DOWN:
            return self.send_json(503, {"error": "mcp down"})
        if auth not in ("ok", "new"):
            return self.send_json(401, {"error": "bad token"}, {"WWW-Authenticate": "Bearer"})
        ident = rpc.get("id")
        if ident is None:  # a notification
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": (rpc.get("params") or {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "fake-hub", "version": "0.0.1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "whoami",
                        "description": "Return the principal this token belongs to.",
                        "inputSchema": {"type": "object", "properties": {}},
                    },
                    {
                        "name": "echo",
                        "description": "Return the given text.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    },
                ]
            }
        elif method == "tools/call":
            params = rpc.get("params") or {}
            name = params.get("name")
            text = (
                "principal=contractor-test project=spike"
                if name == "whoami"
                else str((params.get("arguments") or {}).get("text", ""))
            )
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        elif method == "ping":
            result = {}
        else:
            return self.send_json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": ident,
                    "error": {"code": -32601, "message": "method not found"},
                },
            )
        payload = {"jsonrpc": "2.0", "id": ident, "result": result}
        headers = {"Mcp-Session-Id": "spike-session"} if method == "initialize" else {}
        if MCP_SSE:
            body = f"event: message\ndata: {json.dumps(payload)}\n\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
            return None
        return self.send_json(200, payload, headers)


def main() -> int:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    emit({"kind": "ready", "port": PORT})
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
