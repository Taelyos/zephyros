"""The operator channel: the human's way to administer the hub until the dashboard exists.

It is transitional, logged, and unreachable from an execution context. It takes a token in a
header and nothing else: no cookie, no session. A request that looks like a browser's, or that
names a host other than the ones this channel serves, is refused before any token is read, which
is what stops a web page of the human's from driving it (DNS rebinding, cross-site requests).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from zephyros.adapters.http.common import (
    BodyError,
    bearer,
    not_found,
    peer_address,
    read_json,
    unauthorized,
)
from zephyros.core.audit import HUB_STREAM, Action, Reason
from zephyros.core.hub import Hub
from zephyros.core.tokens import Endpoint

_STATUS: Final = {
    "forbidden": 403,
    "invalid": 400,
    "not_found": 404,
    "conflict": 409,
    "unknown_tool": 404,
    "internal": 500,
}


def _browser_marker(request: Request) -> str | None:
    if "origin" in request.headers:
        return "origin"
    if "cookie" in request.headers:
        return "cookie"
    for name in request.headers:
        lowered = str(name).lower()
        if lowered.startswith("sec-fetch-"):
            return lowered
    return None


def build_operator_app(hub: Hub, *, port: int, extra_hosts: Iterable[str] = ()) -> Starlette:
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}", *extra_hosts}

    def guard(request: Request) -> Response | None:
        """Refuse what no command-line client sends: a browser's markers, a foreign Host."""
        marker = _browser_marker(request)
        host = request.headers.get("host", "").lower()
        if marker is not None or host not in hosts:
            reason = Reason.BROWSER_REQUEST if marker is not None else Reason.BAD_HOST
            hub.log_refusal(
                "unauthenticated",
                HUB_STREAM,
                Action.OPERATOR_CHANNEL,
                reason,
                {"source": peer_address(request) or "", "marker": marker or "", "host": host[:100]},
            )
            return JSONResponse({"error": "refused"}, status_code=403)
        return None

    async def health(request: Request) -> Response:
        refused = guard(request)
        return refused or JSONResponse({"status": "ok"})

    async def call(request: Request) -> Response:
        refused = guard(request)
        if refused is not None:
            return refused
        caller = hub.authenticate(
            bearer(request), peer_address=peer_address(request), endpoint=Endpoint.OPERATOR
        )
        if caller is None:
            return unauthorized()
        try:
            body = await read_json(request)
        except BodyError as error:
            hub.log_caller_refusal(caller, Reason.INVALID_REQUEST, str(error))
            return JSONResponse(
                {"ok": False, "error": {"kind": "invalid", "message": str(error)}}, error.status
            )
        if not isinstance(body, dict) or set(body) - {"tool", "arguments"} or "tool" not in body:
            hub.log_caller_refusal(caller, Reason.INVALID_REQUEST, "the body is {tool, arguments}")
            return JSONResponse(
                {
                    "ok": False,
                    "error": {"kind": "invalid", "message": "the body is {tool, arguments}"},
                },
                status_code=400,
            )
        response = hub.call(caller, body["tool"], body.get("arguments", {}))
        if response.ok:
            return JSONResponse({"ok": True, "data": response.data})
        failure = response.failure
        assert failure is not None  # noqa: S101  # reason: a failed response carries its failure
        return JSONResponse(
            {
                "ok": False,
                "error": {
                    "kind": failure.kind,
                    "reason": failure.reason.value,
                    "message": failure.message,
                    "action": failure.action,
                },
            },
            status_code=_STATUS.get(failure.kind, 403),
        )

    async def fallback(_request: Request) -> Response:
        return not_found()

    return Starlette(
        routes=[
            Route("/operator/health", health, methods=["GET"]),
            Route("/operator/call", call, methods=["POST"]),
            Route("/{path:path}", fallback, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
        ]
    )
