"""The listener of the container filter: one route, strict, on a Unix socket.

Anything that is not `POST /operation` with a small JSON body of a permitted kind is refused
before it reaches the executor. In particular a request shaped like a call to the runtime's API
finds no route: no version prefix, no other version, no encoded path, no chunked body, no method
override. Each refusal goes to the journal.
"""

from __future__ import annotations

from typing import Final

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from zephyros.adapters.filter.executor import Executor
from zephyros.adapters.filter.journal import Journal
from zephyros.adapters.filter.sandbox import MAX_ARCHIVE, read_archive
from zephyros.core.broker import MAX_BODY, BrokerError, parse_body, parse_request

_STATUS: Final = {
    "unknown_operation": 400,
    "bad_request": 400,
    "bad_json": 400,
    "duplicate_key": 400,
    "policy": 400,
    "too_large": 413,
    "not_found": 404,
    "not_zephyros": 403,
    "conflict": 409,
    "hub_missing": 409,
    "hub_not_connected": 409,
    "not_ready": 409,
    "runtime_error": 502,
}
_REFUSED_HEADERS: Final = (
    "transfer-encoding",
    "expect",
    "upgrade",
    "content-encoding",
    "x-http-method-override",
    "x-method-override",
    "x-http-method",
    "forwarded",
    "x-forwarded-for",
)


def _error(code: str, message: str) -> Response:
    return JSONResponse(
        {"ok": False, "error": {"code": code, "message": message}},
        status_code=_STATUS.get(code, 400),
    )


async def _strict_body(request: Request, path: bytes, content_type: str, limit: int) -> bytes:
    """The body of a request written plainly, or BrokerError: the one path, no query, no header
    that changes how the request is read, the one content type, a declared length within the
    limit."""
    if request.scope.get("raw_path", path).split(b"?")[0] != path:
        raise BrokerError("bad_request", "the path is not written plainly")
    if request.url.query:
        raise BrokerError("bad_request", "no query string")
    for name in _REFUSED_HEADERS:
        if name in request.headers:
            raise BrokerError("bad_request", f"the header {name} is not accepted")
    if request.headers.get("content-type", "") != content_type:
        raise BrokerError("bad_request", f"the body is {content_type}")
    declared = request.headers.get("content-length", "")
    if not declared.isdigit():
        raise BrokerError("bad_request", "the body has a length")
    if int(declared) > limit:
        raise BrokerError("too_large", "the body is too large")
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > limit:
            raise BrokerError("too_large", "the body is too large")
    return bytes(received)


def build_filter_app(executor: Executor, journal: Journal) -> Starlette:
    async def operation(request: Request) -> Response:
        try:
            raw = await _strict_body(request, b"/operation", "application/json", MAX_BODY)
            parsed = parse_request(parse_body(raw))
        except BrokerError as error:
            journal.record("refused", "?", error.code)
            return _error(error.code, str(error))
        try:
            result = await run_in_threadpool(executor.perform, parsed)
        except BrokerError as error:
            return _error(error.code, str(error))
        return JSONResponse({"ok": True, "result": result})

    async def sandbox(request: Request) -> Response:
        try:
            raw = await _strict_body(request, b"/sandbox", "application/x-tar", MAX_ARCHIVE)
            manifest, archive = read_archive(raw)
        except BrokerError as error:
            journal.record("refused", "sandbox.run", error.code)
            return _error(error.code, str(error))
        try:
            result = await run_in_threadpool(executor.run_sandbox, manifest, archive)
        except BrokerError as error:
            return _error(error.code, str(error))
        return JSONResponse({"ok": True, "result": result})

    async def fallback(request: Request) -> Response:
        journal.record(
            "refused",
            "?",
            "no_route",
            path=request.url.path.encode("ascii", "backslashreplace").decode()[:80],
        )
        return JSONResponse(
            {"ok": False, "error": {"code": "no_route", "message": "no such route"}},
            status_code=404,
        )

    return Starlette(
        routes=[
            Route("/operation", operation, methods=["POST"]),
            Route("/sandbox", sandbox, methods=["POST"]),
            Route(
                "/{path:path}",
                fallback,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
            ),
        ]
    )
