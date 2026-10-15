"""The agent engines an instance can run, by the name the configuration gives (ADR 0029)."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from zephyros.adapters.hermes.engine import HermesEngine
from zephyros.adapters.opencode.engine import OpenCodeEngine
from zephyros.core.ports.agent_engine import AgentEngine

ENGINES: Mapping[str, Callable[[], AgentEngine]] = {
    "hermes": HermesEngine,
    "opencode": OpenCodeEngine,
}
