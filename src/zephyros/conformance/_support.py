"""Helpers shared by the conformance suites."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from zephyros.core.versioning import parse_version


class ConformanceError(AssertionError):
    """An implementation broke a rule of its interface."""


def check(condition: object, message: str) -> None:
    """Fail the conformance check when `condition` is false. Unlike `assert`, always runs."""
    if not condition:
        raise ConformanceError(message)


def check_interface_version(declared: str, supported: str) -> None:
    """The implementation declares a version of the interface with the major version we test."""
    try:
        declared_major = parse_version(declared)[0]
    except ValueError:
        raise ConformanceError(f"interface_version is not a version number: {declared!r}") from None
    check(
        declared_major == parse_version(supported)[0],
        f"the implementation follows interface {declared}, this suite tests {supported}",
    )


def run_in_loop[T](scenario: Callable[[], Awaitable[T]]) -> T:
    """Run an async scenario in a fresh event loop. Each scenario builds what it uses inside."""

    async def main() -> T:
        return await scenario()

    return asyncio.run(main())
