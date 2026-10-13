"""Conformance suites of the public interfaces (CTR-11).

One suite per interface. Every implementation of an interface, dummy or real, passes the
same suite. A suite imports the core and the standard library only, so that anyone who
implements an interface can import it. See docs/interfaces.md.
"""

from __future__ import annotations

from zephyros.conformance._support import ConformanceError
from zephyros.conformance.agent_engine import AgentEngineConformance
from zephyros.conformance.delivery import DeliveryConformance, DeliveryFixture
from zephyros.conformance.forge import ForgeConformance, ForgeFixture
from zephyros.conformance.model_provider import ModelProviderConformance
from zephyros.conformance.verification import VerificationConformance, VerificationFixture

__all__ = [
    "AgentEngineConformance",
    "ConformanceError",
    "DeliveryConformance",
    "DeliveryFixture",
    "ForgeConformance",
    "ForgeFixture",
    "ModelProviderConformance",
    "VerificationConformance",
    "VerificationFixture",
]
