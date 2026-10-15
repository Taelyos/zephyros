"""The context the agents work with (ADR 0033).

The operator declares the context window of the model the gateway serves
(`gateway.context_window`), and may give the agents less of it (`gateway.context_tokens`). Both
engines compact the conversation at one point derived from the two, early enough that the
provider never refuses a call for its length: Hermes through `compression.threshold_tokens`,
OpenCode through the input limit of its model.

A context below 256 000 tokens is accepted, and said loudly: the real run of 2026-10-15 failed
with 80 000 (the Owner compacted 14 times in 46 minutes and lost the thread of its work).
"""

from __future__ import annotations

from dataclasses import dataclass

CONTEXT_FLOOR = 256_000
"""Below this, the agents work with too little context: accepted, with a warning."""
DEFAULT_CONTEXT_WINDOW = 256_000
"""The window assumed when the operator declares none."""
MINIMUM_CONTEXT_WINDOW = 64_000
"""The smallest window the pinned Hermes Agent starts with."""
MARGIN_TOKENS = 32_000
"""What the compaction leaves free at least below the window: the answer of the model (16 000
tokens at most for OpenCode) and the results of the tools it called."""
MARGIN_SHARE = 10
"""And at least a tenth of the window."""


def margin(window: int) -> int:
    """What stays free between the compaction point and the window of the model."""
    return max(MARGIN_TOKENS, window // MARGIN_SHARE)


@dataclass(frozen=True, slots=True)
class AgentContext:
    """The context window of the model, the context the agents work with, and where they
    compact."""

    window: int = DEFAULT_CONTEXT_WINDOW
    tokens: int = DEFAULT_CONTEXT_WINDOW

    def __post_init__(self) -> None:
        if self.window < MINIMUM_CONTEXT_WINDOW:
            raise ValueError(f"the context window is {MINIMUM_CONTEXT_WINDOW} tokens at least")
        if not 0 < self.tokens <= self.window:
            raise ValueError("the context of the agents is within the window of the model")

    @property
    def compaction(self) -> int:
        """Where the engines compact: the context of the agents, or the window less its margin
        when that comes first."""
        return min(self.tokens, self.window - margin(self.window))

    @property
    def below_floor(self) -> bool:
        return self.tokens < CONTEXT_FLOOR

    def warning(self) -> str | None:
        """What the operator is told, on every surface, when the agents work below the floor."""
        if not self.below_floor:
            return None
        if self.tokens == self.window:
            why = f"the model's context window is {self.window} tokens"
        else:
            why = f"gateway.context_tokens is {self.tokens} (the model's window: {self.window})"
        return (
            f"the agents work with a context of {self.tokens} tokens, below the floor of "
            f"{CONTEXT_FLOOR}: {why}. They compact at {self.compaction} tokens and may lose the "
            "thread of their work: the real run of 2026-10-15 failed with 80 000 (the Owner "
            "compacted 14 times in 46 minutes). See docs/configuration.md, the context of the "
            "agents."
        )


DEFAULT_CONTEXT = AgentContext()
"""The context of a configuration that declares none."""


def context_banner(warning: str) -> str:
    """A warning that cannot be missed in a terminal or in the log of a container."""
    rule = "!" * 78
    return f"{rule}\nWARNING: {warning}\n{rule}"
