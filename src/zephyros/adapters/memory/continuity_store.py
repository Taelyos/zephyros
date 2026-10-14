"""The tasks of the continuity in memory. For tests and simulation."""

from __future__ import annotations

import threading

from zephyros.core.continuity import Expectation


class InMemoryContinuityStore:
    def __init__(self) -> None:
        self._expectations: dict[tuple[str, str], Expectation] = {}
        self._lock = threading.Lock()

    def put(self, expectation: Expectation) -> None:
        with self._lock:
            self._expectations[expectation.key] = expectation

    def remove(self, project_id: str, kind: str) -> None:
        with self._lock:
            self._expectations.pop((project_id, kind), None)

    def all(self) -> list[Expectation]:
        with self._lock:
            return [self._expectations[key] for key in sorted(self._expectations)]
