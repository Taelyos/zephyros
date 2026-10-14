"""The main-agent endpoint: MCP for the main agent, on its own listener.

It accepts the main-agent token and nothing else. No execution context reaches it: it listens on
the hub's core address and its own loopback only, never on an instance network, and it refuses a
peer whose address is one of the instance pool, so an instance finds nothing there even without
its guard (ATK-13, ADR 0025). An expired main-agent token is
told how the human gets a new one (ADR 0026).
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from zephyros.adapters.http.agent import mcp_endpoint
from zephyros.adapters.http.common import from_instance_network, not_found, peer_address
from zephyros.core.audit import HUB_STREAM, Action, Reason
from zephyros.core.hub import Hub
from zephyros.core.tokens import Endpoint


def build_main_agent_app(hub: Hub, *, instance_pool: str | None = None) -> Starlette:
    async def mcp(request: Request) -> Response:
        if from_instance_network(request, instance_pool):
            hub.log_refusal(
                "unauthenticated",
                HUB_STREAM,
                Action.AUTHENTICATION,
                Reason.WRONG_SOURCE,
                {"source": peer_address(request) or "", "endpoint": Endpoint.MAIN_AGENT.value},
            )
            return JSONResponse({"error": "refused"}, status_code=403)
        return await mcp_endpoint(hub, request, Endpoint.MAIN_AGENT)

    async def fallback(_request: Request) -> Response:
        return not_found()

    return Starlette(
        routes=[
            Route("/mcp", mcp, methods=["POST", "GET", "DELETE"]),
            Route("/{path:path}", fallback, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
        ]
    )
