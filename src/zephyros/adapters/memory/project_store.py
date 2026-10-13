"""Projects in memory."""

from __future__ import annotations

from zephyros.core.projects import Project


class InMemoryProjectStore:
    def __init__(self) -> None:
        self._projects: dict[str, Project] = {}

    def add(self, project: Project) -> None:
        if project.id in self._projects:
            raise ValueError("a project with this identifier exists")
        self._projects[project.id] = project

    def get(self, project_id: str) -> Project | None:
        return self._projects.get(project_id)

    def all(self) -> list[Project]:
        return sorted(self._projects.values(), key=lambda project: (project.created_at, project.id))
