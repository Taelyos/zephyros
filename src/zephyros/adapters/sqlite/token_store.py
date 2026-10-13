"""Token records in SQLite. Only the hash of a token is stored."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from zephyros.adapters.sqlite.database import Database
from zephyros.core.clock import format_time, parse_time
from zephyros.core.principals import parse_principal
from zephyros.core.tokens import TokenKind, TokenRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    token_id      TEXT PRIMARY KEY,
    token_hash    TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL,
    principal     TEXT NOT NULL,
    instance_id   TEXT,
    bound_address TEXT,
    issued_at     TEXT NOT NULL,
    expires_at    TEXT,
    revoked_at    TEXT
);
CREATE INDEX IF NOT EXISTS tokens_instance ON tokens (instance_id);
"""

_COLUMNS = (
    "token_id, token_hash, kind, principal, instance_id, bound_address, "
    "issued_at, expires_at, revoked_at"
)


def _time(value: str | None) -> datetime | None:
    return None if value is None else parse_time(value)


def _record(row: sqlite3.Row) -> TokenRecord:
    return TokenRecord(
        token_id=row["token_id"],
        token_hash=row["token_hash"],
        kind=TokenKind(row["kind"]),
        principal=parse_principal(row["principal"]),
        instance_id=row["instance_id"],
        bound_address=row["bound_address"],
        issued_at=parse_time(row["issued_at"]),
        expires_at=_time(row["expires_at"]),
        revoked_at=_time(row["revoked_at"]),
    )


class SqliteTokenStore:
    def __init__(self, database: Database) -> None:
        self._db = database
        database.script(SCHEMA)

    def add(self, record: TokenRecord) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                f"INSERT INTO tokens ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.token_id,
                    record.token_hash,
                    record.kind.value,
                    record.principal.id,
                    record.instance_id,
                    record.bound_address,
                    format_time(record.issued_at),
                    None if record.expires_at is None else format_time(record.expires_at),
                    None if record.revoked_at is None else format_time(record.revoked_at),
                ),
            )

    def get_by_hash(self, token_hash: str) -> TokenRecord | None:
        rows = self._db.query(f"SELECT {_COLUMNS} FROM tokens WHERE token_hash = ?", (token_hash,))
        return _record(rows[0]) if rows else None

    def get(self, token_id: str) -> TokenRecord | None:
        rows = self._db.query(f"SELECT {_COLUMNS} FROM tokens WHERE token_id = ?", (token_id,))
        return _record(rows[0]) if rows else None

    def revoke(self, token_id: str, at: datetime) -> bool:
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "UPDATE tokens SET revoked_at = ? WHERE token_id = ? AND revoked_at IS NULL",
                (format_time(at), token_id),
            )
            return cursor.rowcount == 1

    def for_instance(self, instance_id: str) -> list[TokenRecord]:
        rows = self._db.query(
            f"SELECT {_COLUMNS} FROM tokens WHERE instance_id = ? ORDER BY issued_at",
            (instance_id,),
        )
        return [_record(row) for row in rows]
