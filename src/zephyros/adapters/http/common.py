"""What the three listeners share: reading a bounded body and a bearer, answering JSON."""

from __future__ import annotations

import json
from typing import Final

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from zephyros.core.ports.delivery import MAX_PACKAGE as DELIVERY_MAX_PACKAGE

MAX_BODY: Final = 1024 * 1024
MAX_PACKAGE: Final = DELIVERY_MAX_PACKAGE


class BodyError(Exception):
    """The body is missing, too large, or not JSON."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


def bearer(request: Request) -> str | None:
    """The token of an `Authorization: Bearer ...` header, or None."""
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def peer_address(request: Request) -> str | None:
    """The TCP peer of the connection. Never a header: a forwarded address is the caller's claim."""
    return request.client.host if request.client is not None else None


async def read_json(request: Request) -> object:
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        shown = content_type[:40] if content_type.isascii() and content_type.isprintable() else "?"
        raise BodyError(415, f"the body must be application/json, not {shown or 'none'}")
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY):
        raise BodyError(413, "the body is too large")
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > MAX_BODY:
            raise BodyError(413, "the body is too large")
    try:
        return json.loads(bytes(received))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BodyError(400, "the body is not valid JSON") from error


async def read_package(request: Request) -> bytes:
    """A package (a git bundle) as the body: octet-stream or a git bundle, bounded."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in ("application/octet-stream", "application/x-git-bundle"):
        raise BodyError(415, "the body is a package (application/x-git-bundle)")
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_PACKAGE):
        raise BodyError(413, "the package is too large")
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > MAX_PACKAGE:
            raise BodyError(413, "the package is too large")
    if not received:
        raise BodyError(400, "the package is empty")
    return bytes(received)


def unauthorized() -> Response:
    """The same answer for every authentication failure: it says nothing about why."""
    return JSONResponse(
        {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
    )


def not_found() -> Response:
    return JSONResponse({"error": {"message": "not found"}}, status_code=404)
