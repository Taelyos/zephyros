"""Projects in SQLite."""

from __future__ import annotations

import sqlite3

from zephyros.adapters.sqlite.database import Database
from zephyros.core.clock import format_time, parse_time
from zephyros.core.projects import Project

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id     TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    repository_url TEXT,
    created_at     TEXT NOT NULL,
    created_by     TEXT NOT NULL
);
"""


def _project(row: sqlite3.Row) -> Project:
    return Project(
        id=row["project_id"],
        name=row["name"],
        repository_url=row["repository_url"],
        created_at=parse_time(row["created_at"]),
        created_by=row["created_by"],
    )


class SqliteProjectStore:
    def __init__(self, database: Database) -> None:
        self._db = database
        database.script(SCHEMA)

    def add(self, project: Project) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO projects (project_id, name, repository_url, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    project.id,
                    project.name,
                    project.repository_url,
                    format_time(project.created_at),
                    project.created_by,
                ),
            )

    def get(self, project_id: str) -> Project | None:
        rows = self._db.query("SELECT * FROM projects WHERE project_id = ?", (project_id,))
        return _project(rows[0]) if rows else None

    def all(self) -> list[Project]:
        return [
            _project(row)
            for row in self._db.query("SELECT * FROM projects ORDER BY created_at, project_id")
        ]
