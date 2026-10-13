"""The registry of public interface versions."""

from __future__ import annotations

from zephyros.core.ports import INTERFACE_VERSIONS
from zephyros.core.versioning import parse_version


def test_lists_the_five_public_interfaces() -> None:
    assert set(INTERFACE_VERSIONS) == {
        "AgentEngine",
        "ModelProvider",
        "Forge",
        "DeliveryArea",
        "VerificationRunner",
    }


def test_every_version_is_a_version_number_before_one() -> None:
    for name, version in INTERFACE_VERSIONS.items():
        assert parse_version(version)[0] == 0, f"{name} is no longer 0.x: update the notice"
