"""The filter's own journal: one JSON line per request, performed or refused.

It is plain and append-only, with no chain: the filter has no clock or store of the hub's. It
never holds a token or a prompt, only what was asked and what became of it.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Protocol


class Journal(Protocol):
    def record(self, outcome: str, op: str, code: str, **details: str) -> None: ...


class FileJournal:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        self._lock = threading.Lock()

    def record(self, outcome: str, op: str, code: str, **details: str) -> None:
        line = json.dumps(
            {
                "time": int(time.time()),
                "outcome": outcome,
                "op": op[:40],
                "code": code,
                **{k: v[:80] for k, v in details.items()},
            },
            sort_keys=True,
            ensure_ascii=True,
        )
        with self._lock:
            os.write(self._descriptor, (line + "\n").encode("ascii"))

    def close(self) -> None:
        os.close(self._descriptor)


class MemoryJournal:
    def __init__(self) -> None:
        self.lines: list[dict[str, str]] = []

    def record(self, outcome: str, op: str, code: str, **details: str) -> None:
        self.lines.append({"outcome": outcome, "op": op, "code": code, **details})
