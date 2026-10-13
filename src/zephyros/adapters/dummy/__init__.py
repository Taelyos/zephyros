"""Dummy implementations of the public interfaces (CTR-11).

They answer without a real service and pass the same conformance suites as the real ones.
"""

from __future__ import annotations

from zephyros.adapters.dummy.agent_engine import DummyAgentEngine
from zephyros.adapters.dummy.forge import InMemoryForge
from zephyros.adapters.dummy.model_provider import ScriptedModelProvider

__all__ = ["DummyAgentEngine", "InMemoryForge", "ScriptedModelProvider"]
