"""The dummy agent engine describes a turn that echoes its prompt, and passes the suite."""

from __future__ import annotations

from zephyros.adapters.dummy import DummyAgentEngine
from zephyros.core.ports.agent_engine import Side, TurnRequest


def test_describes_a_turn_that_echoes() -> None:
    request = TurnRequest("p_a", Side.OWNER, "http://hub.invalid:80", "m", "zph_o_t", "hi")
    description = DummyAgentEngine().describe_turn(request)
    assert description.command == ("echo", "hi")
    assert dict(description.environment) == {
        "ZEPHYROS_ENDPOINT": "http://hub.invalid:80",
        "ZEPHYROS_MODEL": "m",
        "ZEPHYROS_TOKEN": "zph_o_t",
    }
    assert DummyAgentEngine().engine_version == "dummy-0"
    assert DummyAgentEngine().interface_version == "0.2.0"
