"""The obstacles in SQLite (ADR 0034): one row each, its fields as JSON, so a restart finds them."""

from __future__ import annotations

from zephyros.adapters.sqlite.database import Database
from zephyros.core.obstacles import Obstacle, deserialise, serialise

SCHEMA = """
CREATE TABLE IF NOT EXISTS obstacles (
    project_id TEXT    NOT NULL,
    number     INTEGER NOT NULL,
    status     TEXT    NOT NULL,
    level      TEXT    NOT NULL,
    data       TEXT    NOT NULL,
    PRIMARY KEY (project_id, number)
) WITHOUT ROWID;
"""


class SqliteObstacleStore:
    def __init__(self, database: Database) -> None:
        self._db = database
        database.script(SCHEMA)

    def add(self, obstacle: Obstacle) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO obstacles (project_id, number, status, level, data) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    obstacle.project_id,
                    obstacle.number,
                    obstacle.status.value,
                    obstacle.level.value,
                    serialise(obstacle),
                ),
            )

    def put(self, obstacle: Obstacle) -> None:
        with self._db.transaction() as connection:
            changed = connection.execute(
                "UPDATE obstacles SET status = ?, level = ?, data = ? "
                "WHERE project_id = ? AND number = ?",
                (
                    obstacle.status.value,
                    obstacle.level.value,
                    serialise(obstacle),
                    obstacle.project_id,
                    obstacle.number,
                ),
            ).rowcount
            if changed != 1:
                raise ValueError(f"there is no {obstacle.id} to replace")

    def get(self, project_id: str, number: int) -> Obstacle | None:
        rows = self._db.query(
            "SELECT data FROM obstacles WHERE project_id = ? AND number = ?",
            (project_id, number),
        )
        return deserialise(rows[0]["data"]) if rows else None

    def of_project(self, project_id: str) -> list[Obstacle]:
        rows = self._db.query(
            "SELECT data FROM obstacles WHERE project_id = ? ORDER BY number", (project_id,)
        )
        return [deserialise(row["data"]) for row in rows]
