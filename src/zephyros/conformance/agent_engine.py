"""Conformance suite of the agent engine interface.

To check an implementation, subclass `AgentEngineConformance`, implement `make_engine`, and
let pytest collect the `test_` methods (name the subclass `Test...`).
"""

from __future__ import annotations

import re

from zephyros.conformance._support import check, check_interface_version
from zephyros.core.ports.agent_engine import (
    INTERFACE_VERSION,
    AgentEngine,
    Side,
    TurnDescription,
    TurnRequest,
)

_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
TOKEN_ONE = "zph_c_conformance-token-one"
TOKEN_TWO = "zph_c_conformance-token-two"
ENDPOINT = "http://hub.invalid:8080"
HOSTILE = "--profile=evil -p x; rm -rf / $(id) `id`\nsecond line\t--continue"


def _request(token: str = TOKEN_ONE, prompt: str = "Build the thing.") -> TurnRequest:
    return TurnRequest(
        project_id="p_conformance",
        side=Side.CONTRACTOR,
        agent_endpoint=ENDPOINT,
        model="zephyros-default",
        token=token,
        prompt=prompt,
    )


class AgentEngineConformance:
    """Rules every agent engine follows."""

    def make_engine(self) -> AgentEngine:
        raise NotImplementedError

    def test_follows_the_tested_major_version(self) -> None:
        check_interface_version(self.make_engine().interface_version, INTERFACE_VERSION)

    def test_reports_the_engine_version(self) -> None:
        check(self.make_engine().engine_version.strip(), "engine_version is empty")

    def test_describes_the_turn_with_a_command(self) -> None:
        description = self.make_engine().describe_turn(_request())
        check(isinstance(description, TurnDescription), "not a TurnDescription")
        check(description.command, "the command is empty")
        check(all(description.command), "the command has an empty word")

    def test_gives_the_same_description_for_the_same_request(self) -> None:
        engine = self.make_engine()
        check(
            engine.describe_turn(_request()) == engine.describe_turn(_request()),
            "two descriptions of the same request differ",
        )

    def test_hands_the_token_through_the_environment_only(self) -> None:
        description = self.make_engine().describe_turn(_request())
        check(TOKEN_ONE in description.environment.values(), "the token does not reach the turn")
        check(not any(TOKEN_ONE in word for word in description.command), "the token is a word")
        holders = [name for name, value in description.environment.items() if TOKEN_ONE in value]
        check(len(holders) == 1, f"the token is in several variables: {holders}")

    def test_hands_the_agent_endpoint_and_the_model_to_the_turn(self) -> None:
        values = list(self.make_engine().describe_turn(_request()).environment.values())
        check(any(ENDPOINT in value for value in values), "the agent endpoint is not handed over")
        check("zephyros-default" in values, "the model is not handed over")

    def test_keeps_the_prompt_whole_in_one_word(self) -> None:
        description = self.make_engine().describe_turn(_request(prompt=HOSTILE))
        holders = [word for word in description.command if HOSTILE in word]
        check(len(holders) == 1, "the prompt is not exactly one word of the command")
        rest = [word for word in description.command if HOSTILE not in word]
        check(not any(part in rest for part in ("--profile=evil", "-p", "x;")), "it was split")

    def test_depends_on_the_token_of_the_request(self) -> None:
        engine = self.make_engine()
        engine.describe_turn(_request(TOKEN_ONE))
        values = engine.describe_turn(_request(TOKEN_TWO)).environment.values()
        check(TOKEN_TWO in values, "the new token is lost")
        check(not any(TOKEN_ONE in value for value in values), "an earlier token is still there")

    def test_names_variables_portably_and_holds_no_nul(self) -> None:
        description = self.make_engine().describe_turn(_request())
        for name, value in description.environment.items():
            check(_NAME.fullmatch(name), f"not a portable variable name: {name!r}")
            check("\x00" not in value, f"{name} holds a NUL")
        check(not any("\x00" in word for word in description.command), "a word holds a NUL")
