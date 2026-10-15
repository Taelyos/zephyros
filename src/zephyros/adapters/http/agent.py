"""The agent endpoint: the one hub destination open to instances. It serves `/mcp`, the model
endpoint `/v1`, and the staging area: `GET /staging/base` (the base of the project, as a
package), `POST /staging/delivery?branch=<name>` (a pushed branch, as a package; the Contractor
only) and `GET /staging/delivery/<delivery_id>` (a delivered branch, as a package, for the Owner
to read what it judges; ADR 0020). Each is one call of the hub, with one record.

By a decision of the project owner (2026-10-10), `GET /v1/models` answers without a token with a
fixed, non-secret list, because Hermes asks for it bare after every turn. The metadata probes that
Hermes sends before its first call get 404, silently. Nothing else is served without a token.
"""

from __future__ import annotations

import re
from typing import Final

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from zephyros.adapters.http.common import (
    BodyError,
    bearer,
    not_found,
    peer_address,
    read_json,
    read_package,
    token_refusal,
    unauthorized,
)
from zephyros.adapters.http.mcp import handle_message
from zephyros.core.audit import Reason
from zephyros.core.gateway import GatewayError, GatewayService, render_stream
from zephyros.core.hub import Hub, ToolFailure
from zephyros.core.tokens import Endpoint, Rejected

BUNDLE_TYPE: Final = "application/x-git-bundle"
METADATA_PROBES: Final = frozenset(
    {"/api/v1/models", "/api/tags", "/api/show", "/props", "/v1/props", "/version"}
)


DELIVERY_ID: Final = re.compile(r"d[0-9]{1,6}")


async def mcp_endpoint(hub: Hub, request: Request, endpoint: Endpoint) -> Response:
    """POST, GET and DELETE on /mcp, for the agent endpoint and the main-agent endpoint."""
    caller = hub.identify(bearer(request), peer_address=peer_address(request), endpoint=endpoint)
    if isinstance(caller, Rejected):
        return token_refusal(caller)
    if request.method in ("GET", "HEAD"):
        # Hermes' MCP client probes with HEAD, then GET, before it posts: no stream is offered.
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})
    if request.method == "DELETE":
        return JSONResponse({})
    try:
        message = await read_json(request)
    except BodyError as error:
        hub.log_caller_refusal(caller, Reason.INVALID_REQUEST, str(error))
        return JSONResponse({"error": str(error)}, status_code=error.status)
    status, body = handle_message(hub, caller, message)
    if body is None:
        return Response(status_code=status)
    return JSONResponse(body, status_code=status)


def build_agent_app(
    hub: Hub, *, model_name: str, gateway: GatewayService | None = None
) -> Starlette:
    async def mcp(request: Request) -> Response:
        return await mcp_endpoint(hub, request, Endpoint.AGENT)

    async def models(_request: Request) -> Response:
        listing = {
            "object": "list",
            "data": [{"id": model_name, "object": "model", "owned_by": "zephyros"}],
        }
        return JSONResponse(listing)

    async def chat(request: Request) -> Response:
        """The model gateway: an OpenAI chat call with a project token."""
        if gateway is None:
            return not_found()
        caller = hub.authenticate(
            bearer(request), peer_address=peer_address(request), endpoint=Endpoint.AGENT
        )
        if caller is None:
            return unauthorized()
        try:
            body = await read_json(request, gateway.limits.body_bytes)
            answer = await gateway.complete(caller, body)
        except BodyError as error:
            hub.log_caller_refusal(caller, Reason.INVALID_REQUEST, str(error))
            return JSONResponse(
                {
                    "error": {
                        "message": str(error),
                        "type": "invalid_request_error",
                        "code": "bad_body",
                    }
                },
                status_code=error.status,
            )
        except GatewayError as error:
            return JSONResponse(error.body(), status_code=error.status)
        if answer.stream:
            events = render_stream(answer.completion, include_usage=answer.include_usage)
            return StreamingResponse(iter(events), media_type="text/event-stream")
        return JSONResponse(answer.completion)

    def failure_response(failure: ToolFailure | None) -> Response:
        status = {"forbidden": 403, "not_found": 404, "conflict": 409}.get(
            failure.kind if failure else "", 400
        )
        body: dict[str, str] = {"error": failure.message if failure else "refused"}
        if failure is not None:
            body["action"] = failure.action
        return JSONResponse(body, status_code=status)

    async def staging_base(request: Request) -> Response:
        caller = hub.authenticate(
            bearer(request), peer_address=peer_address(request), endpoint=Endpoint.AGENT
        )
        if caller is None:
            return unauthorized()
        package, failure = await run_in_threadpool(hub.staging_base, caller)
        if package is None:
            return failure_response(failure)
        return Response(package, media_type=BUNDLE_TYPE)

    async def staging_delivery_read(request: Request) -> Response:
        caller = hub.authenticate(
            bearer(request), peer_address=peer_address(request), endpoint=Endpoint.AGENT
        )
        if caller is None:
            return unauthorized()
        delivery_id = str(request.path_params["delivery_id"])
        if not DELIVERY_ID.fullmatch(delivery_id):
            hub.log_caller_refusal(caller, Reason.INVALID_REQUEST, "not a delivery identifier")
            return JSONResponse({"error": "not a delivery identifier"}, status_code=400)
        package, failure = await run_in_threadpool(hub.staging_delivery, caller, delivery_id)
        if package is None:
            return failure_response(failure)
        return Response(package, media_type=BUNDLE_TYPE)

    async def staging_delivery(request: Request) -> Response:
        caller = hub.authenticate(
            bearer(request), peer_address=peer_address(request), endpoint=Endpoint.AGENT
        )
        if caller is None:
            return unauthorized()
        branch = request.query_params.get("branch", "")
        try:
            package = await read_package(request)
        except BodyError as error:
            hub.log_caller_refusal(caller, Reason.INVALID_REQUEST, str(error))
            return JSONResponse({"error": str(error)}, status_code=error.status)
        record, failure = await run_in_threadpool(hub.push_delivery, caller, branch, package)
        if record is None:
            return failure_response(failure)
        return JSONResponse(
            {
                "delivery_id": record.delivery_id,
                "branch": record.branch,
                "changed_paths": list(record.changed_paths),
                "next": "declare it with the tool delivery_declare",
            }
        )

    async def fallback(request: Request) -> Response:
        token = bearer(request)
        if token is not None and request.url.path not in METADATA_PROBES:
            caller = hub.authenticate(
                token, peer_address=peer_address(request), endpoint=Endpoint.AGENT
            )
            if caller is not None:
                hub.log_caller_refusal(caller, Reason.NOT_FOUND, "no such path")
        return not_found()

    return Starlette(
        routes=[
            Route("/mcp", mcp, methods=["POST", "GET", "DELETE"]),
            Route("/v1/models", models, methods=["GET"]),
            Route("/v1/chat/completions", chat, methods=["POST"]),
            Route("/staging/base", staging_base, methods=["GET"]),
            Route("/staging/delivery", staging_delivery, methods=["POST"]),
            Route("/staging/delivery/{delivery_id}", staging_delivery_read, methods=["GET"]),
            Route(
                "/{path:path}",
                fallback,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
            ),
        ]
    )
