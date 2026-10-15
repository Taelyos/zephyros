"""The context the agents work with (ADR 0033): where they compact, and when the hub warns."""

from __future__ import annotations

from pathlib import Path

import pytest

from zephyros.core.context import (
    CONTEXT_FLOOR,
    DEFAULT_CONTEXT,
    FLOOR_REASON,
    AgentContext,
    context_banner,
    margin,
)
from zephyros.core.ports.agent_engine import Side, TurnRequest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("window", "tokens", "compaction"),
    [
        (1_000_000, 1_000_000, 900_000),  # the whole window, less a tenth
        (1_000_000, 400_000, 400_000),  # less than the window: no margin needed
        (1_000_000, 256_000, 256_000),
        (256_000, 256_000, 224_000),  # the default: 32 000 tokens at least stay free
        (128_000, 128_000, 96_000),
        (64_000, 64_000, 32_000),
    ],
)
def test_the_agents_compact_at_their_context_or_before_the_end_of_the_window(
    window: int, tokens: int, compaction: int
) -> None:
    context = AgentContext(window, tokens)
    assert context.compaction == compaction
    assert window - context.compaction >= margin(window)  # room for the answer and the tools


def test_the_default_is_a_window_of_256_000_used_whole() -> None:
    assert (DEFAULT_CONTEXT.window, DEFAULT_CONTEXT.tokens) == (256_000, 256_000)
    assert DEFAULT_CONTEXT.compaction == 224_000


@pytest.mark.parametrize(
    ("window", "tokens"), [(64_000 - 1, 64_000 - 1), (256_000, 300_000), (256_000, 0)]
)
def test_a_context_outside_the_window_is_refused(window: int, tokens: int) -> None:
    with pytest.raises(ValueError, match="window"):
        AgentContext(window, tokens)


def test_at_the_floor_or_above_nothing_is_said_of_a_declared_window() -> None:
    for context in (AgentContext(1_000_000, CONTEXT_FLOOR), AgentContext(1_000_000, 1_000_000)):
        assert not context.below_floor
        assert context.warnings(declared=True, model=True) == []


def test_the_floor_is_about_where_the_agents_compact() -> None:
    """The default window of 256 000 tokens compacts at 224 000: below the floor (review of
    PR 22, point 1)."""
    assert DEFAULT_CONTEXT.below_floor
    [warning] = DEFAULT_CONTEXT.warnings(declared=True, model=True)
    assert "compact at 224000 tokens, below the floor of 256000" in warning
    assert "the model's context window is 256000 tokens" in warning
    assert FLOOR_REASON in warning


def test_a_context_given_below_the_floor_names_the_key() -> None:
    [warning] = AgentContext(1_000_000, 80_000).warnings(declared=True, model=True)
    assert "gateway.context_tokens is 80000" in warning
    assert "a long task loses the thread of its work" in warning
    assert "2026" not in warning


def test_a_window_not_declared_for_a_real_model_is_a_guess_and_said_so() -> None:
    warnings = DEFAULT_CONTEXT.warnings(declared=False, model=True)
    assert len(warnings) == 2
    assert warnings[0].startswith("gateway.context_window is not declared")


def test_the_stub_has_no_context_to_lose() -> None:
    assert AgentContext(64_000, 64_000).warnings(declared=False, model=False) == []


def test_the_defaults_of_the_engines_and_their_images_are_the_hubs() -> None:
    """The same defaults live in the interface of the engines and in both entrypoints: a turn
    started without the figures behaves as the hub's default."""
    request = TurnRequest("p", Side.OWNER, "http://hub.invalid", "m", "zph_o_t", "go")
    assert (request.context_window, request.compaction_tokens) == (
        DEFAULT_CONTEXT.window,
        DEFAULT_CONTEXT.compaction,
    )
    for image in ("instance", "instance-opencode"):
        text = (ROOT / "images" / image / "entrypoint.sh").read_text(encoding="utf-8")
        assert f"ZEPHYROS_CONTEXT_WINDOW={DEFAULT_CONTEXT.window} ;;" in text, image
        assert f"ZEPHYROS_COMPACTION_TOKENS={DEFAULT_CONTEXT.compaction} ;;" in text, image


def test_the_banner_cannot_be_missed() -> None:
    lines = context_banner(["too small", "a guess"]).splitlines()
    assert lines[0] == lines[3] == "!" * 78
    assert lines[1:3] == ["WARNING: too small", "WARNING: a guess"]
