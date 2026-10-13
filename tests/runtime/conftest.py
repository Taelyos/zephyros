"""Fixtures of the runtime tests: the images, built once, and one world per module."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.runtime.rig import ROOT, Runtime, build


@pytest.fixture(scope="session")
def images() -> dict[str, str]:
    return {
        "guard": build("zephyros-guard:test", ROOT / "images" / "guard"),
        "probe": build("zephyros-test-probe:test", ROOT / "tests/runtime/images/probe"),
        "observer": build("zephyros-test-observer:test", ROOT / "tests/runtime/images/observer"),
        "dns": build("zephyros-test-dns:test", ROOT / "tests/runtime/images/dns"),
    }


@pytest.fixture(scope="module")
def runtime(images: dict[str, str]) -> Iterator[Runtime]:
    world = Runtime(images)
    world.setup()
    try:
        yield world
    finally:
        world.teardown()
