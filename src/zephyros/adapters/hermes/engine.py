"""Hermes Agent as the agent engine: one non-interactive run per turn (ADR 0016).

The instance image (images/instance) seeds the configuration of Hermes from three environment
variables at every start: the hub's URL (the model gateway and the MCP server are both there),
the model name and the project token. A turn is a one-shot chat in the session named
`zephyros`, kept in the home volume: created by the first turn, resumed by every later one.
(A bare `--continue` fails when there is no session yet, which is the case of the first turn.)

The prompt is attached to its option in one word. Hermes reads its command line twice, once
before argparse (to take out `--profile`) and once with argparse; as one word starting with
`--query=`, a prompt is never read as an option by either, whatever it begins with.
"""

from __future__ import annotations

from zephyros.core.ports.agent_engine import INTERFACE_VERSION, TurnDescription, TurnRequest

HERMES_VERSION = "0.21.6 (818c13be)"
SESSION = "zephyros"


class HermesEngine:
    @property
    def interface_version(self) -> str:
        return INTERFACE_VERSION

    @property
    def engine_version(self) -> str:
        return HERMES_VERSION

    def describe_turn(self, request: TurnRequest) -> TurnDescription:
        return TurnDescription(
            command=(
                "chat",
                f"--query={request.prompt}",
                "--oneshot",
                f"--continue={SESSION}",
                "--create-if-missing",
                "--quiet",
            ),
            environment={
                "ZEPHYROS_HUB_URL": request.agent_endpoint,
                "ZEPHYROS_MODEL": request.model,
                "ZEPHYROS_TOKEN": request.token,
            },
        )
