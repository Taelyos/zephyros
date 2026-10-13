"""The public interfaces of the hub (CTR-11).

The core knows an agent engine, a model provider, a forge, a delivery area and a verification
runner only through these interfaces. Each carries a version number, is documented in
docs/interfaces.md, has a dummy implementation in `zephyros.adapters.dummy`, and has a
conformance suite in `zephyros.conformance` that every implementation passes.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from zephyros.core.ports import agent_engine, delivery, forge, model_provider, verification

INTERFACE_VERSIONS: Mapping[str, str] = MappingProxyType(
    {
        "AgentEngine": agent_engine.INTERFACE_VERSION,
        "ModelProvider": model_provider.INTERFACE_VERSION,
        "Forge": forge.INTERFACE_VERSION,
        "DeliveryArea": delivery.INTERFACE_VERSION,
        "VerificationRunner": verification.INTERFACE_VERSION,
    }
)
