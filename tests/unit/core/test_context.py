"""The context the agents work with (ADR 0033): where they compact, and when the hub warns."""

from __future__ import annotations

import pytest

from zephyros.core.context import (
    CONTEXT_FLOOR,
    DEFAULT_CONTEXT,
    AgentContext,
    context_banner,
    margin,
)


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
    assert DEFAULT_CONTEXT.warning() is None


@pytest.mark.parametrize(
    ("window", "tokens"), [(64_000 - 1, 64_000 - 1), (256_000, 300_000), (256_000, 0)]
)
def test_a_context_outside_the_window_is_refused(window: int, tokens: int) -> None:
    with pytest.raises(ValueError, match="window"):
        AgentContext(window, tokens)


def test_at_the_floor_or_above_nothing_is_said() -> None:
    assert AgentContext(1_000_000, CONTEXT_FLOOR).warning() is None
    assert not AgentContext(1_000_000, CONTEXT_FLOOR).below_floor


def test_below_the_floor_the_warning_names_the_key_and_the_failed_run() -> None:
    warning = AgentContext(1_000_000, 80_000).warning()
    assert warning is not None
    assert "gateway.context_tokens is 80000" in warning
    assert "below the floor of 256000" in warning
    assert "2026-10-15" in warning
    assert "80 000" in warning


def test_a_model_smaller_than_the_floor_is_named_as_the_limit() -> None:
    warning = AgentContext(128_000, 128_000).warning()
    assert warning is not None
    assert "the model's context window is 128000 tokens" in warning
    assert "compact at 96000" in warning


def test_the_banner_cannot_be_missed() -> None:
    lines = context_banner("too small").splitlines()
    assert lines[0] == lines[2] == "!" * 78
    assert lines[1] == "WARNING: too small"
