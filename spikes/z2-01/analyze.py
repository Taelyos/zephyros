"""Host-side reader of the fake hub log for spike Z2-01.

    docker logs fake-hub 2>&1 | python3 analyze.py COMMAND

Reads the JSON lines the fake hub prints (other lines are ignored) and prints one answer.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from typing import Any


def records() -> list[dict[str, Any]]:
    rows = []
    for line in sys.stdin:
        if line.startswith("{"):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def main_chats(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r["kind"] == "chat" and r.get("n_tools")]


def where(row: dict[str, Any]) -> str:
    return f"{row['method']} {row['path']}"


def main() -> int:
    command = sys.argv[1]
    rows = records()
    if command == "count":
        kind = sys.argv[2]
        print(sum(1 for r in rows if r["kind"] == kind))
    elif command == "clients":
        print(",".join(sorted({r["client"] for r in rows if r["kind"] != "ready"})))
    elif command == "auth-summary":
        counts = Counter(r["auth"] for r in rows if r["kind"] != "ready")
        print(" ".join(f"{k}={counts.get(k, 0)}" for k in ("ok", "new", "wrong", "missing")))
    elif command == "missing-paths":
        print(", ".join(sorted({where(r) for r in rows if r.get("auth") == "missing"})) or "none")
    elif command == "bad-auth-outside-models":
        # requests without a good bearer, other than the one GET /v1/models Hermes sends bare
        bad = [
            where(r)
            for r in rows
            if r["kind"] != "ready"
            and r["auth"] not in ("ok", "new")
            and where(r) != "GET /v1/models"
        ]
        print(len(bad))
    elif command == "paths":
        counts = Counter(where(r) for r in rows if r["kind"] != "ready")
        for path, number in sorted(counts.items()):
            print(f"{number:3d}  {path}")
    elif command == "mcp-auth":
        states = Counter(r["auth"] for r in rows if r["kind"].startswith("mcp"))
        print(" ".join(f"{k}={v}" for k, v in sorted(states.items())) or "none")
    elif command == "mcp-all-ok":
        mcp = [r for r in rows if r["kind"].startswith("mcp")]
        print("yes" if mcp and all(r["auth"] in ("ok", "new") for r in mcp) else "no")
    elif command == "plugin-check":
        # "<pid the plugin ran in> <pid of the Hermes process> <plugin host processes listed>"
        text = "\n".join(r["last_tool_result"] for r in rows if r.get("last_tool_result")).replace(
            "\\n", "\n"
        )
        plugin = re.search(r'"plugin_pid": (\d+)', text)
        hermes = re.search(r"^\s*(\d+)\s+\d+\s+/opt/hermes/.venv/bin/python -P", text, re.M)
        print(
            plugin.group(1) if plugin else "?",
            hermes.group(1) if hermes else "?",
            text.count("plugin_host_child"),
        )
    elif command == "mcp-rpc":
        print(" ".join(str(r.get("rpc_method")) for r in rows if r["kind"] == "mcp"))
    elif command == "tool-names":
        chats = main_chats(rows)
        print(",".join(chats[0]["tool_names"]) if chats else "none")
    elif command == "tool-count":
        chats = main_chats(rows)
        print(chats[0]["n_tools"] if chats else 0)
    elif command == "tool-results":
        for r in rows:
            if r["kind"] == "chat" and r.get("last_tool_result"):
                print(r["last_tool_result"])
                print("--------")
    elif command == "streams":
        chats = main_chats(rows)
        print(",".join(str(c["stream"]).lower() for c in chats) or "none")
    elif command == "stream-options":
        chats = main_chats(rows)
        print(json.dumps(chats[0].get("stream_options")) if chats else "none")
    elif command == "messages":
        print(",".join(str(c["n_messages"]) for c in main_chats(rows)) or "none")
    elif command == "aux-chats":
        print(sum(1 for r in rows if r["kind"] == "chat" and not r.get("n_tools")))
    else:
        print(f"unknown command {command}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
