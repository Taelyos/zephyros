"""A dummy agent engine: describes a turn that only echoes its prompt."""

from __future__ import annotations

from zephyros.core.ports.agent_engine import INTERFACE_VERSION, TurnDescription, TurnRequest


class DummyAgentEngine:
    """Describes an inert turn. It stands in for a real engine in tests and probes."""

    @property
    def interface_version(self) -> str:
        return INTERFACE_VERSION

    @property
    def engine_version(self) -> str:
        return "dummy-0"

    def describe_turn(self, request: TurnRequest) -> TurnDescription:
        return TurnDescription(
            command=("echo", request.prompt),
            environment={
                "ZEPHYROS_ENDPOINT": request.agent_endpoint,
                "ZEPHYROS_MODEL": request.model,
                "ZEPHYROS_TOKEN": request.token,
            },
        )
