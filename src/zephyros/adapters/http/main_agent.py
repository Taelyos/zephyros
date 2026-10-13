"""The main-agent endpoint: MCP for the main agent, on its own listener.

It accepts the main-agent token and nothing else, and no execution context can reach it: it is
bound to an address the instance networks do not carry, and the network guard blocks the rest.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from zephyros.adapters.http.agent import mcp_endpoint
from zephyros.adapters.http.common import not_found
from zephyros.core.hub import Hub
from zephyros.core.tokens import Endpoint


def build_main_agent_app(hub: Hub) -> Starlette:
    async def mcp(request: Request) -> Response:
        return await mcp_endpoint(hub, request, Endpoint.MAIN_AGENT)

    async def fallback(_request: Request) -> Response:
        return not_found()

    return Starlette(
        routes=[
            Route("/mcp", mcp, methods=["POST", "GET", "DELETE"]),
            Route("/{path:path}", fallback, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
        ]
    )
