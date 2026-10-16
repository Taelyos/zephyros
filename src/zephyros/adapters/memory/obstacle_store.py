"""The obstacles in memory. For tests and simulation."""

from __future__ import annotations

import threading

from zephyros.core.obstacles import Obstacle


class InMemoryObstacleStore:
    def __init__(self) -> None:
        self._obstacles: dict[tuple[str, int], Obstacle] = {}
        self._lock = threading.Lock()

    def add(self, obstacle: Obstacle) -> None:
        key = (obstacle.project_id, obstacle.number)
        with self._lock:
            if key in self._obstacles:
                raise ValueError(f"{obstacle.id} exists already")
            self._obstacles[key] = obstacle

    def put(self, obstacle: Obstacle) -> None:
        key = (obstacle.project_id, obstacle.number)
        with self._lock:
            if key not in self._obstacles:
                raise ValueError(f"there is no {obstacle.id} to replace")
            self._obstacles[key] = obstacle

    def get(self, project_id: str, number: int) -> Obstacle | None:
        with self._lock:
            return self._obstacles.get((project_id, number))

    def of_project(self, project_id: str) -> list[Obstacle]:
        with self._lock:
            return [self._obstacles[key] for key in sorted(self._obstacles) if key[0] == project_id]
