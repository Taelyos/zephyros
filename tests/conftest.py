"""Tests that need a container runtime run only when asked, so that `scripts/check.sh` works on
a machine without one. Set ZEPHYROS_RUNTIME_TESTS=1 to run them (scripts/acceptance.sh does)."""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("ZEPHYROS_RUNTIME_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="needs a container runtime: set ZEPHYROS_RUNTIME_TESTS=1")
    for item in items:
        if item.get_closest_marker("runtime") or item.get_closest_marker("integration"):
            item.add_marker(skip)
