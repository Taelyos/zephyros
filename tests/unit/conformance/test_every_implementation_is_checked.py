"""US-E11-06: no implementation of a public interface escapes its conformance suite."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import pkgutil
from collections.abc import Iterable

import pytest

import zephyros.adapters
from tests.unit.conformance import implementations
from zephyros.conformance import (
    AgentEngineConformance,
    DeliveryConformance,
    ForgeConformance,
    ModelProviderConformance,
    VerificationConformance,
)


def find_implementations() -> set[type]:
    """Every class under `zephyros.adapters` that declares an `interface_version`."""
    found: set[type] = set()
    for info in pkgutil.walk_packages(zephyros.adapters.__path__, prefix="zephyros.adapters."):
        module = importlib.import_module(info.name)
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ == module.__name__ and hasattr(cls, "interface_version"):
                found.add(cls)
    return found


def unchecked(found: Iterable[type], checked: Iterable[type]) -> set[type]:
    return set(found) - set(checked)


def test_every_implementation_under_adapters_has_a_conformance_run() -> None:
    missing = sorted(
        cls.__qualname__ for cls in unchecked(find_implementations(), implementations.CHECKED)
    )
    assert missing == [], (
        f"add a conformance run to tests/unit/conformance/implementations.py: {missing}"
    )


def test_the_list_holds_no_class_that_is_gone() -> None:
    stale = set(implementations.CHECKED) - find_implementations()
    assert stale == set()


def test_the_guard_notices_a_missing_run() -> None:
    class Newcomer:
        interface_version = "0.1.0"

    assert unchecked({Newcomer}, implementations.CHECKED) == {Newcomer}
    assert unchecked({Newcomer}, {Newcomer}) == set()


def test_the_discovery_sees_the_implementations() -> None:
    assert {cls.__name__ for cls in find_implementations()} == {
        "AssertionRunner",
        "DirectoryForge",
        "DummyAgentEngine",
        "GitDeliveryArea",
        "GitHubForge",
        "HermesEngine",
        "InMemoryDeliveryArea",
        "InMemoryForge",
        "ObedientStubProvider",
        "OpenAiCompatibleProvider",
        "OpenCodeEngine",
        "SandboxRunner",
        "ScriptedModelProvider",
    }


@pytest.mark.parametrize(("implementation", "suite"), list(implementations.CHECKED.items()))
def test_each_listed_run_really_builds_its_implementation(
    implementation: type, suite: type
) -> None:
    instance = suite()
    if issubclass(suite, AgentEngineConformance):
        built = instance.make_engine()
    elif issubclass(suite, ModelProviderConformance):
        built = asyncio.run(instance.make_provider())
    elif issubclass(suite, ForgeConformance):
        built = asyncio.run(instance.make_fixture()).forge
    elif issubclass(suite, DeliveryConformance):
        built = instance.make_fixture().area
    elif issubclass(suite, VerificationConformance):
        built = instance.make_fixture().runner
    else:
        pytest.fail(f"{suite.__name__} is not a conformance suite")
    assert type(built) is implementation
