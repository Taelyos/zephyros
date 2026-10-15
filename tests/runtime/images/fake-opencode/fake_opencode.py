#!/usr/local/bin/python3
"""A fake `opencode run`: it does with the configuration what OpenCode does, without a model.

It takes the command line and the standard input the entrypoint gives it, reads the managed
configuration and fills its {env:NAME} the way OpenCode does (tests/runtime/test_opencode.py
checks that the real OpenCode resolves it to the same values), then calls the hub as OpenCode
would: one call of the model gateway with the provider's base URL and key, and the MCP tools
that the prompt names on its lines `TOOL zephyros_<tool> <json>`. Its session is a file of the
home volume, so a turn sees the prompts of the turns before it, as `--continue` does.

It writes what it saw to /work/fake-opencode-<turn>.json: its command line, the names of its
environment, its mount points, every file and environment variable in which the canary marker
of the test appears, and the answers of the hub.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CONFIG = Path("/etc/opencode/opencode.jsonc")
SESSION = Path.home() / ".local/share/opencode/fake-session.json"
MARKER = ("zephyros" + "-canary-").encode()  # never whole in this file, which is scanned too
SKIPPED = ("/proc", "/sys", "/dev")
TOOL_LINE = re.compile(r"^TOOL zephyros_([a-z_]+) (\{.*\})$", re.MULTILINE)


def configuration() -> dict[str, Any]:
    lines = CONFIG.read_text(encoding="utf-8").splitlines()
    text = "\n".join(line for line in lines if not line.lstrip().startswith("//"))
    text = re.sub(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: os.environ.get(m[1], ""), text)
    found: dict[str, Any] = json.loads(text)
    return found


def post(url: str, headers: dict[str, str], body: object) -> tuple[int, str]:
    request = urllib.request.Request(  # noqa: S310  # reason: the hub's URL, from the configuration
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as answer:  # noqa: S310  # reason: as above
            return answer.status, answer.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def marked() -> list[str]:
    """Every environment variable and readable file in which the marker appears."""
    found = [f"env:{name}" for name, value in os.environ.items() if MARKER in value.encode()]
    for root, directories, files in os.walk("/"):
        if root.startswith(SKIPPED):
            directories[:] = []
            continue
        for name in files:
            path = os.path.join(root, name)
            try:
                if os.path.islink(path) or os.path.getsize(path) > 16 * 1024 * 1024:
                    continue
                with open(path, "rb") as handle:
                    if MARKER in handle.read():
                        found.append(path)
            except OSError:
                continue
    return found


def main() -> int:
    prompt = sys.stdin.read()
    config = configuration()
    session = json.loads(SESSION.read_text()) if SESSION.exists() else []
    session.append(prompt)
    SESSION.parent.mkdir(parents=True, exist_ok=True)
    SESSION.write_text(json.dumps(session))

    provider = config["provider"]["zephyros"]["options"]
    model = str(config["model"]).split("/", 1)[1]
    gateway = post(
        f"{provider['baseURL']}/chat/completions",
        {"Authorization": f"Bearer {provider['apiKey']}"},
        {"model": model, "messages": [{"role": "user", "content": prompt}]},
    )
    server = config["mcp"]["zephyros"]
    tools: list[dict[str, object]] = []
    for number, (tool, arguments) in enumerate(TOOL_LINE.findall(prompt), start=1):
        status, body = post(
            server["url"],
            server["headers"],
            {
                "jsonrpc": "2.0",
                "id": number,
                "method": "tools/call",
                "params": {"name": tool, "arguments": json.loads(arguments)},
            },
        )
        tools.append({"tool": tool, "status": status, "answer": json.loads(body)})

    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        after = fields[fields.index("-") + 1 :]
        mounts.append({"point": fields[4], "root": fields[3], "type": after[0]})
    report = {
        "argv": sys.argv[1:],
        "turn": len(session),
        "session": session,
        "environment": sorted(os.environ),
        "mounts": mounts,
        "marked": marked(),
        "gateway": {"status": gateway[0], "model": model},
        "tools": tools,
    }
    Path(f"/work/fake-opencode-{len(session)}.json").write_text(json.dumps(report, indent=1))
    print(f"fake opencode: turn {len(session)} done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
