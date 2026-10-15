"""The agent engine interface: what to run for one turn of an instance.

The agent engine is the software that runs an agent in an instance (Hermes Agent in version 1).
Each piece of work is one turn: one container, started from the instance image, in the network
namespace of the instance's guard, with the instance's two volumes (ADR 0016). The container
filter builds that container from its own templates (ADR 0014); the engine only says which
command and which environment start a turn of it. A description therefore holds nothing that
widens a container: no image (the image is configured), no user, mount, capability, network,
limit or file.

Version 0.2: a turn takes its configuration from the image and its first prompt on the command
line, as the pinned Hermes Agent does; the files of version 0.1 are gone.

Version 0.3: a request says whether the model wants its reasoning back (`reasoning_echo`, from
`gateway.reasoning_echo`), so that the engine keeps the reasoning of each answer in the
conversation it sends again, whatever name the model has behind the gateway (ADR 0032); the
context window of the model and the point where the engine compacts (ADR 0033); and whether
the turn starts a new session of the instance instead of continuing the last one (ADR 0016).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

INTERFACE_VERSION = "0.3.0"
MAX_PROMPT = 4000


class Side(StrEnum):
    """The side of a project that an instance belongs to."""

    OWNER = "owner"
    CONTRACTOR = "contractor"


@dataclass(frozen=True, slots=True)
class TurnRequest:
    """What the hub and the filter know about a turn they want to start."""

    project_id: str
    side: Side
    agent_endpoint: str
    """The base URL of the hub's agent endpoint, as the instance reaches it."""
    model: str
    """The name of the model the instance asks the gateway for."""
    token: str = field(repr=False)
    prompt: str = field(repr=False)
    reasoning_echo: bool = False
    """Whether the model wants the reasoning of its answers back with the conversation."""
    context_window: int = 256_000
    """The context window of the model, in tokens."""
    compaction_tokens: int = 224_000
    """Where the engine compacts the conversation, in tokens: below the window."""
    new_session: bool = False
    """The turn starts a new session of the instance; the earlier ones stay, no longer
    continued."""

    def __post_init__(self) -> None:
        for name in ("project_id", "agent_endpoint", "model", "token", "prompt"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if not 0 < self.compaction_tokens < self.context_window:
            raise ValueError("the compaction comes before the end of the context window")
        if len(self.prompt) > MAX_PROMPT or "\x00" in self.prompt:
            raise ValueError("the prompt is too long or holds a NUL")


@dataclass(frozen=True, slots=True)
class TurnDescription:
    """What starts a turn: the words of a command and the environment. The mapping is read-only
    once the description is built."""

    command: tuple[str, ...]
    environment: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", tuple(self.command))
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))


class AgentEngine(Protocol):
    """Describes the turns of one agent engine, in one pinned version."""

    @property
    def interface_version(self) -> str:
        """The version of this interface that the implementation follows."""
        ...

    @property
    def engine_version(self) -> str:
        """The version of the agent engine that instances run (the pinned one)."""
        ...

    def describe_turn(self, request: TurnRequest) -> TurnDescription:
        """Describe the turn for `request`. Same request, same description."""
        ...
