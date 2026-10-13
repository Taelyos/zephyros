"""Types of the agent engine interface."""

from __future__ import annotations

import pytest

from zephyros.core.ports.agent_engine import MAX_PROMPT, Side, TurnDescription, TurnRequest


def _request(**changes: str) -> TurnRequest:
    values = {
        "project_id": "p",
        "agent_endpoint": "http://hub.invalid",
        "model": "m",
        "token": "zph_o_secret-value",
        "prompt": "private prompt",
    }
    values.update(changes)
    return TurnRequest(side=Side.OWNER, **values)


class TestTurnRequest:
    @pytest.mark.parametrize("field", ["project_id", "agent_endpoint", "model", "token", "prompt"])
    def test_refuses_an_empty_field(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            _request(**{field: ""})

    @pytest.mark.parametrize("prompt", ["x" * (MAX_PROMPT + 1), "a\x00b"])
    def test_refuses_a_prompt_too_long_or_with_a_nul(self, prompt: str) -> None:
        with pytest.raises(ValueError, match="prompt"):
            _request(prompt=prompt)

    def test_does_not_show_the_token_or_the_prompt_when_printed(self) -> None:
        text = repr(_request())
        assert "secret-value" not in text
        assert "private prompt" not in text
        assert "project_id='p'" in text

    def test_sides_are_owner_and_contractor(self) -> None:
        assert {side.value for side in Side} == {"owner", "contractor"}


class TestTurnDescription:
    def test_copies_and_freezes(self) -> None:
        environment = {"A": "1"}
        command = ["run"]
        description = TurnDescription(tuple(command), environment)
        environment["A"] = "changed"
        assert dict(description.environment) == {"A": "1"}
        assert description.command == ("run",)
        with pytest.raises(TypeError):
            description.environment["B"] = "2"  # type: ignore[index]  # reason: the test proves it is read-only
