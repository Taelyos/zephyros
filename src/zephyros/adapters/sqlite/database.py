"""The database of the hub: one SQLite file, one connection, serialized by a lock."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class Database:
    """A SQLite connection shared by the stores. `path` may be ':memory:' in tests."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._connection = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA foreign_keys = ON")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A transaction that commits on success and rolls back on any error."""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def query(self, sql: str, parameters: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._connection.execute(sql, parameters).fetchall()

    def script(self, sql: str) -> None:
        """Run schema statements (CREATE ... IF NOT EXISTS)."""
        with self._lock:
            self._connection.executescript(sql)

    def close(self) -> None:
        with self._lock:
            self._connection.close()
