"""The command line of Zephyros.

zephyros config check FILE | schema
zephyros hub start --config FILE
zephyros hub call TOOL [--args JSON]      the operator channel of a running hub
zephyros hub rotate-token KIND            a new operator or main-agent token (ADR 0026)
zephyros filter start --config FILE       the container filter
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

import httpx

from zephyros import __version__
from zephyros.adapters.config_file import load_config
from zephyros.core.config import CONFIG_SCHEMA_VERSION, ConfigError
from zephyros.core.context import context_banner
from zephyros.core.publication import published_schemas


def _config_check(path: Path) -> int:
    try:
        config = load_config(path)
    except ConfigError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(f"{path}: valid (configuration schema {CONFIG_SCHEMA_VERSION})")
    context = config.gateway.context
    print(
        f"the agents work with {context.tokens} tokens of a window of {context.window}, and "
        f"compact at {context.compaction}"
    )
    warnings = config.gateway.context_warnings
    if warnings:
        # Accepted, and said where scripts/deploy.sh shows it (ADR 0033).
        print(context_banner(warnings), file=sys.stderr)
    return 0


def _tools_check(path: Path) -> int:
    """What the hub checks of a tools directory when it starts (ADR 0030), and its fingerprint:
    the one the log and the evidence report give, so that a verdict can be replayed with the
    same tools."""
    from zephyros.adapters.project_tools import ToolsDirectory
    from zephyros.core.tools import ToolsError

    try:
        measured = ToolsDirectory(path).digest()
    except ToolsError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"tools": measured.fingerprint, "files": measured.files, "bytes": measured.bytes}
        )
    )
    return 0


def _config_schema() -> int:
    sys.stdout.write(published_schemas()["configuration.schema.json"])
    return 0


DEFAULT_URL = "http://127.0.0.1:8081"
DEFAULT_TOKEN_FILE = "/var/lib/zephyros/operator.token"  # noqa: S105  # reason: a path, not a secret


def run_call(client: httpx.Client, url: str, token: str, tool: str, arguments: object) -> int:
    """Call one tool on the operator channel and print the answer. 0 when it succeeded."""
    response = client.post(
        f"{url.rstrip('/')}/operator/call",
        json={"tool": tool, "arguments": arguments},
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        body = response.json()
    except ValueError:
        body = {
            "ok": False,
            "error": {"kind": "invalid", "message": f"HTTP {response.status_code}"},
        }
    print(json.dumps(body, indent=2, sort_keys=True))
    return 0 if response.status_code == 200 and body.get("ok") is True else 1


def _hub_call(args: argparse.Namespace) -> int:
    try:
        arguments = json.loads(args.args)
        token = Path(args.token_file).read_text(encoding="ascii").strip()
    except (ValueError, OSError) as error:
        print(f"cannot read the arguments or the token file: {error}", file=sys.stderr)
        return 2
    with httpx.Client(timeout=30) as client:
        return run_call(client, args.url, token, args.tool, arguments)


DEFAULT_CONFIG = "/etc/zephyros/zephyros.toml"


def run_rotation(client: httpx.Client, kind: str, socket: str = "") -> int:
    """Ask the running hub for a new token of `kind`, on its control socket; print the answer.
    The answer names the file the new token is in, never the token."""
    try:
        response = client.post("http://hub/tokens/rotate", json={"kind": kind})
        body = response.json()
    except (httpx.HTTPError, ValueError) as error:
        where = f" {socket}" if socket else ""
        print(f"the hub did not answer on its control socket{where}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(body, indent=2, sort_keys=True))
    return 0 if response.status_code == 200 and body.get("ok") is True else 1


def _hub_rotate(args: argparse.Namespace) -> int:
    """The socket is `--socket`, or else `paths.control_socket` of the configuration."""
    socket_path = args.socket
    if socket_path is None:
        try:
            socket_path = load_config(Path(args.config)).paths.control_socket
        except ConfigError as error:
            print(f"cannot read the configuration {args.config}: {error}", file=sys.stderr)
            return 2
    transport = httpx.HTTPTransport(uds=socket_path)
    with httpx.Client(transport=transport, timeout=30) as client:
        return run_rotation(client, args.kind, socket_path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zephyros")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config", help="the configuration of the hub")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    check = config_commands.add_parser("check", help="validate a configuration file")
    check.add_argument("file", type=Path)
    config_commands.add_parser("schema", help="print the JSON Schema of the configuration")
    tools = commands.add_parser("tools", help="the tools directory of the projects")
    tools_commands = tools.add_subparsers(dest="tools_command", required=True)
    tools_check = tools_commands.add_parser(
        "check", help="check a tools directory as the hub does, and print its fingerprint"
    )
    tools_check.add_argument("directory", type=Path)
    hub = commands.add_parser("hub", help="run the hub, or call its operator channel")
    hub_commands = hub.add_subparsers(dest="hub_command", required=True)
    start = hub_commands.add_parser("start", help="run the hub")
    start.add_argument("--config", type=Path, required=True)
    call = hub_commands.add_parser("call", help="call a tool as the operator")
    call.add_argument("tool")
    call.add_argument("--args", default="{}", help="the arguments, as a JSON object")
    call.add_argument("--url", default=DEFAULT_URL)
    call.add_argument("--token-file", default=DEFAULT_TOKEN_FILE)
    rotate = hub_commands.add_parser(
        "rotate-token",
        help="issue a new operator or main-agent token into its file, and revoke the old one",
    )
    rotate.add_argument("kind", choices=["operator", "main-agent"])
    rotate.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help="the configuration of the hub, whose paths.control_socket is the socket",
    )
    rotate.add_argument("--socket", help="the control socket, instead of the configuration's")
    filter_ = commands.add_parser("filter", help="run the container filter")
    filter_commands = filter_.add_subparsers(dest="filter_command", required=True)
    filter_start = filter_commands.add_parser("start", help="run the filter")
    filter_start.add_argument("--config", type=Path, required=True)
    filter_start.add_argument("--docker-socket", default="/var/run/docker.sock")
    args = parser.parse_args(argv)
    if args.command == "filter":
        from zephyros.app.filter import main as filter_main

        return filter_main(["--config", str(args.config), "--docker-socket", args.docker_socket])
    if args.command == "hub":
        if args.hub_command == "call":
            return _hub_call(args)
        if args.hub_command == "rotate-token":
            return _hub_rotate(args)
        from zephyros.app.hub import main as hub_main

        return hub_main(["--config", str(args.config)])
    if args.command == "tools":
        return _tools_check(args.directory.absolute())
    if args.config_command == "check":
        return _config_check(args.file)
    return _config_schema()


if __name__ == "__main__":
    raise SystemExit(main())
