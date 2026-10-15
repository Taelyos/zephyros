"""OpenCode as the agent engine: one non-interactive run per turn (ADR 0016, ADR 0029).

The instance image (images/instance-opencode) carries the configuration of OpenCode in its
managed scope, read-only, and OpenCode fills it from three environment variables: the hub's URL
(the model gateway and the MCP server are both there), the model name and the project token.
A turn is `turn <prompt>`: the entrypoint of the image hands the prompt to `opencode run
--continue` on its standard input, so the turn continues the last session of the work
directory, kept on the home volume; the first turn creates it.

The prompt is one word of its own after `turn`, never parsed as an option: the entrypoint reads
exactly two words and passes the second one through a here-document.
"""

from __future__ import annotations

from zephyros.core.ports.agent_engine import INTERFACE_VERSION, TurnDescription, TurnRequest

OPENCODE_VERSION = "1.19.0"


class OpenCodeEngine:
    @property
    def interface_version(self) -> str:
        return INTERFACE_VERSION

    @property
    def engine_version(self) -> str:
        return OPENCODE_VERSION

    def describe_turn(self, request: TurnRequest) -> TurnDescription:
        return TurnDescription(
            command=("turn", request.prompt),
            environment={
                "ZEPHYROS_HUB_URL": request.agent_endpoint,
                "ZEPHYROS_MODEL": request.model,
                "ZEPHYROS_TOKEN": request.token,
            },
        )
