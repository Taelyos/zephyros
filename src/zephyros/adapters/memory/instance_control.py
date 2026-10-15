"""An instance control that only remembers what it was asked. For tests and simulation."""

from __future__ import annotations

from zephyros.core.instances import InstanceInfo, InstanceState
from zephyros.core.ports.agent_engine import Side


class FakeInstanceControl:
    def __init__(self) -> None:
        self._states: dict[tuple[str, Side], InstanceState] = {}
        self.prompts: list[tuple[str, Side, str]] = []
        self.new_sessions: list[tuple[str, Side]] = []

    def start(
        self, project_id: str, side: Side, prompt: str, *, new_session: bool = False
    ) -> InstanceInfo:
        self._states[(project_id, side)] = InstanceState.TURN
        self.prompts.append((project_id, side, prompt))
        if new_session:
            self.new_sessions.append((project_id, side))
        return self.status(project_id, side)

    def stop(self, project_id: str, side: Side) -> InstanceInfo:
        self._states.pop((project_id, side), None)
        return self.status(project_id, side)

    def status(self, project_id: str, side: Side) -> InstanceInfo:
        return InstanceInfo(
            project_id, side, self._states.get((project_id, side), InstanceState.ABSENT)
        )
