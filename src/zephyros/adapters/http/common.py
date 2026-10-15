"""What the three listeners share: reading a bounded body and a bearer, answering JSON."""

from __future__ import annotations

import ipaddress
import json
from typing import Final

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from zephyros.core.audit import Reason
from zephyros.core.clock import format_time
from zephyros.core.ports.delivery import MAX_PACKAGE as DELIVERY_MAX_PACKAGE
from zephyros.core.principals import MAIN_AGENT, OPERATOR
from zephyros.core.tokens import Rejected, normalize_address

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


def from_instance_network(request: Request, pool: str | None) -> bool:
    """True when the TCP peer is an address of the instance pool, that is, of an instance
    network: the operator channel and the main-agent endpoint refuse it before anything else,
    whatever runtime routed it there (ATK-13, ADR 0025). An unknown peer counts as one."""
    if pool is None:
        return False
    peer = normalize_address(peer_address(request))
    if peer is None:
        return True
    address = ipaddress.ip_address(peer)
    network = ipaddress.ip_network(pool)
    return address.version == network.version and address in network


async def read_json(request: Request, max_body: int = MAX_BODY) -> object:
    """The JSON body of `request`, of `max_body` bytes at most."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        shown = content_type[:40] if content_type.isascii() and content_type.isprintable() else "?"
        raise BodyError(415, f"the body must be application/json, not {shown or 'none'}")
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > max_body):
        raise BodyError(413, "the body is too large")
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > max_body:
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


ROTATE_COMMAND: Final = "scripts/deploy.sh rotate-token"
_EXPIRED_NEXT: Final = {
    OPERATOR.id: (
        f"on the machine of the hub, run `{ROTATE_COMMAND} operator`: it writes a new token "
        "to the operator token file, which `zephyros hub call` reads"
    ),
    MAIN_AGENT.id: (
        f"the human runs `{ROTATE_COMMAND} main-agent` on the machine of the hub, then gives "
        "the main agent the new token from the main-agent token file"
    ),
}


def token_refusal(rejected: Rejected) -> Response:
    """The answer to a refused token. Every refusal is `unauthorized()`, save one: the holder of
    an expired operator or main-agent token is told so, and what to do (ADR 0026). It learns
    nothing it did not know: it presented that very token."""
    principal = rejected.principal.id if rejected.principal else ""
    next_step = _EXPIRED_NEXT.get(principal)
    if rejected.reason is not Reason.EXPIRED or next_step is None:
        return unauthorized()
    when = format_time(rejected.expires_at) if rejected.expires_at else "an earlier time"
    message = f"the {principal} token expired at {when}; {next_step}"
    return JSONResponse(
        {"error": "token_expired", "message": message},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
    )


def not_found() -> Response:
    return JSONResponse({"error": {"message": "not found"}}, status_code=404)
