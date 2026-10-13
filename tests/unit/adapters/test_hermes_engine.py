"""Hermes as the agent engine: the command and environment of a turn."""

from __future__ import annotations

import pytest

from zephyros.adapters.hermes.engine import HermesEngine
from zephyros.core.ports.agent_engine import Side, TurnRequest


def request(prompt: str) -> TurnRequest:
    return TurnRequest("p_a", Side.CONTRACTOR, "http://10.200.0.3:8080", "m", "zph_c_t", prompt)


TAIL = ("--oneshot", "--continue=zephyros", "--create-if-missing", "--quiet")


def test_a_turn_is_one_shot_in_the_session_of_the_instance() -> None:
    description = HermesEngine().describe_turn(request("Build it."))
    assert description.command == ("chat", "--query=Build it.", *TAIL)
    assert dict(description.environment) == {
        "ZEPHYROS_HUB_URL": "http://10.200.0.3:8080",
        "ZEPHYROS_MODEL": "m",
        "ZEPHYROS_TOKEN": "zph_c_t",
    }


@pytest.mark.parametrize("prompt", ["-p evil", "--profile=x", "--help", "-", "--"])
def test_a_prompt_that_looks_like_an_option_stays_attached_to_its_option(prompt: str) -> None:
    command = HermesEngine().describe_turn(request(prompt)).command
    assert command == ("chat", f"--query={prompt}", *TAIL)
