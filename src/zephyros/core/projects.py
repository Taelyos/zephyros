"""Projects: a name, a repository URL kept as inert text, and an identifier the hub issues.

The identifier is the only thing that ever reaches a container specification or a file name.
The name and the URL are data (ATK-74): they are stored, shown and logged as text, and no
step builds a command, a path or an option from them.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from zephyros.core.audit import Reason
from zephyros.core.clock import Clock
from zephyros.core.errors import Refusal
from zephyros.core.principals import Principal
from zephyros.core.schema import Field, Table, Text

MAX_NAME = 100
MAX_URL = 2048

CREATE_INPUT = Table(
    (
        Field(
            "name",
            Text(min_length=1, max_length=MAX_NAME),
            "The name of the project",
            required=True,
        ),
        Field(
            "repository_url",
            Text(max_length=MAX_URL),
            "The repository, kept as inert text; its validation is a later milestone",
        ),
    )
)


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    name: str
    repository_url: str | None
    created_at: datetime
    created_by: str


class ProjectStore(Protocol):
    def add(self, project: Project) -> None: ...

    def get(self, project_id: str) -> Project | None: ...

    def all(self) -> list[Project]: ...


class ProjectService:
    def __init__(
        self,
        store: ProjectStore,
        clock: Clock,
        *,
        new_id: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._new_id = new_id or (lambda: "p_" + secrets.token_hex(5))

    def create(self, creator: Principal, fields: Mapping[str, object]) -> Project:
        """Create a project from validated fields. Everything but the identifier is text."""
        project = Project(
            id=self._new_id(),
            name=str(fields["name"]),
            repository_url=None
            if fields.get("repository_url") is None
            else str(fields["repository_url"]),
            created_at=self._clock.now(),
            created_by=creator.id,
        )
        self._store.add(project)
        return project

    def get(self, project_id: str) -> Project:
        project = self._store.get(project_id)
        if project is None:
            raise Refusal(Reason.NOT_FOUND, "there is no such project")
        return project

    def visible_to(self, principal: Principal) -> list[Project]:
        """The projects a principal may see: all of them, or its own."""
        projects = self._store.all()
        if principal.project_id is None:
            return projects
        return [project for project in projects if project.id == principal.project_id]
