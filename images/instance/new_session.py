"""Start a new session of the instance, for a turn the operator asked to begin anew (ADR 0016).

A turn of Hermes continues the session named `zephyros` (src/zephyros/adapters/hermes/engine.py:
`--continue=zephyros --create-if-missing`). Hermes resolves that name to the latest session of
its lineage, `zephyros #N`: this creates the next one, empty, so the turn starts there. The
earlier sessions stay in the database of the home volume: no later turn continues them, but the
`session_search` tool of Hermes can still find them. What Hermes keeps beside the conversation
(its memory, MEMORY.md and SOUL.md, and its skills) is not touched, and the new session reads it.

Run by the entrypoint with the Python of Hermes, before Hermes. Without any session yet, there is
nothing to leave behind: the turn creates `zephyros` as a first turn does.
"""

from __future__ import annotations

from hermes_state_ids import new_session_id
from hermes_state_registry import acquire

SESSION = "zephyros"


def main() -> None:
    db = acquire()
    try:
        title = db.get_next_title_in_lineage(SESSION)
        if title != SESSION:
            identifier = new_session_id()
            db.create_session(identifier, source="cli")
            db.set_session_title(identifier, title)
    finally:
        db.close()


if __name__ == "__main__":
    main()
