"""The MCP side of the hub: Streamable HTTP, stateless, JSON answers only.

The hub serves tools. A request is one JSON-RPC message in the body of a POST; the answer is
one JSON object. There is no session, no server-initiated stream and no batch, so a GET is
answered 405. Hermes Agent, whose behavior spike Z2-01 measured, works against exactly this.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from zephyros.core.hub import Caller, Hub, ToolResponse, ToolSpec

HUB_NAME = "zephyros-hub"
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")


def _error(ident: object, code: int, message: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


def _tool_description(spec: ToolSpec) -> dict[str, object]:
    return {
        "name": spec.mcp_name,
        "description": spec.description,
        "inputSchema": spec.inputs.json_schema(),
    }


def _tool_result(response: ToolResponse) -> dict[str, object]:
    if response.ok:
        text = json.dumps(response.data, sort_keys=True)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    failure = response.failure
    assert failure is not None  # noqa: S101  # reason: a failed response carries its failure
    body = {"error": failure.kind, "reason": failure.reason.value, "message": failure.message}
    return {
        "content": [{"type": "text", "text": json.dumps(body, sort_keys=True)}],
        "isError": True,
    }


def handle_message(
    hub: Hub, caller: Caller, message: object
) -> tuple[int, dict[str, object] | None]:
    """Answer one JSON-RPC message: an HTTP status and a body (None for a notification)."""
    if not isinstance(message, Mapping) or message.get("jsonrpc") != "2.0":
        return 400, _error(None, JSONRPC_INVALID_REQUEST, "not a JSON-RPC 2.0 message")
    method = message.get("method")
    ident = message.get("id")
    if not isinstance(method, str):
        return 400, _error(ident, JSONRPC_INVALID_REQUEST, "no method")
    if "id" not in message:  # a notification: nothing to answer
        return 202, None
    params = message.get("params")
    params = params if isinstance(params, Mapping) else {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return 200, {
            "jsonrpc": "2.0",
            "id": ident,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": HUB_NAME, "version": "0.0.0"},
            },
        }
    if method == "ping":
        return 200, {"jsonrpc": "2.0", "id": ident, "result": {}}
    if method == "tools/list":
        tools = [_tool_description(spec) for spec in hub.tools_for(caller)]
        return 200, {"jsonrpc": "2.0", "id": ident, "result": {"tools": tools}}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments", {})
        response = hub.call(caller, name, arguments)
        if (
            not response.ok
            and response.failure is not None
            and response.failure.kind == "unknown_tool"
        ):
            return 200, _error(ident, JSONRPC_INVALID_PARAMS, "unknown tool")
        return 200, {"jsonrpc": "2.0", "id": ident, "result": _tool_result(response)}
    return 200, _error(ident, JSONRPC_METHOD_NOT_FOUND, "method not found")
