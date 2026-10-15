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
        "ZEPHYROS_REASONING_ECHO": "false",
        "ZEPHYROS_CONTEXT_WINDOW": "256000",
        "ZEPHYROS_COMPACTION_TOKENS": "224000",
        "ZEPHYROS_SESSION": "continue",
    }


def test_a_turn_says_whether_the_model_wants_its_reasoning_back() -> None:
    """ADR 0032: the image turns it into `model.reasoning_echo`, whatever the model's name."""
    wanted = TurnRequest(
        "p_a", Side.CONTRACTOR, "http://10.200.0.3:8080", "served-model", "zph_c_t", "Go.", True
    )
    assert HermesEngine().describe_turn(wanted).environment["ZEPHYROS_REASONING_ECHO"] == "true"


@pytest.mark.parametrize("prompt", ["-p evil", "--profile=x", "--help", "-", "--"])
def test_a_prompt_that_looks_like_an_option_stays_attached_to_its_option(prompt: str) -> None:
    command = HermesEngine().describe_turn(request(prompt)).command
    assert command == ("chat", f"--query={prompt}", *TAIL)


def test_a_turn_carries_the_context_and_the_session_it_was_given() -> None:
    """ADR 0033 and ADR 0016: the image turns them into the configuration of the engine."""
    given = TurnRequest(
        "p_a",
        Side.CONTRACTOR,
        "http://10.200.0.3:8080",
        "served-model",
        "zph_c_t",
        "Go.",
        context_window=1_000_000,
        compaction_tokens=900_000,
        new_session=True,
    )
    environment = HermesEngine().describe_turn(given).environment
    assert environment["ZEPHYROS_CONTEXT_WINDOW"] == "1000000"
    assert environment["ZEPHYROS_COMPACTION_TOKENS"] == "900000"
    assert environment["ZEPHYROS_SESSION"] == "new"
