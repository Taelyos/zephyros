"""Run a conformance suite by hand and report which of its checks failed."""

from __future__ import annotations

from zephyros.conformance import ConformanceError


def failed_checks(suite: object) -> set[str]:
    """Names of the `test_` methods of `suite` that raise ConformanceError."""
    failed: set[str] = set()
    for name in dir(suite):
        if not name.startswith("test_"):
            continue
        try:
            getattr(suite, name)()
        except ConformanceError:
            failed.add(name)
    return failed


def all_checks(suite: object) -> set[str]:
    return {name for name in dir(suite) if name.startswith("test_")}
