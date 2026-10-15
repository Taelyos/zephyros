"""The session of the instance: the root session OpenCode last wrote, read from its database on
the home volume (ADR 0029). Printed for the entrypoint, which continues it with `--session`.

`opencode run --continue` takes the last session of the project OpenCode names the work
directory after, and that name follows the first commit of the git repository there: a turn that
clones another repository into /work, or starts its history again, would make the next turn
begin a new conversation. The session is looked up across every project instead.

Nothing is printed, and the entrypoint falls back to `--continue`, when there is no database yet
(the first turn) or it cannot be read; the reason goes to the standard error of the turn.
"""

import os
import re
import sqlite3
import sys
from pathlib import Path

SESSION = re.compile(r"ses_[A-Za-z0-9]{1,64}")
QUERY = (
    "select id from session where parent_id is null and time_archived is null "
    "order by time_updated desc, id desc limit 1"
)


def main() -> None:
    database = Path(os.environ["HOME"]) / ".local" / "share" / "opencode" / "opencode.db"
    if not database.is_file():
        return
    try:
        with sqlite3.connect(database) as connection:
            row = connection.execute(QUERY).fetchone()
    except sqlite3.Error as error:
        print(f"zephyros: the session of the instance is unreadable: {error}", file=sys.stderr)
        return
    if row is not None and isinstance(row[0], str) and SESSION.fullmatch(row[0]):
        print(row[0])


main()
