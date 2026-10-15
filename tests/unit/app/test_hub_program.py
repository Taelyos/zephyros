"""The hub program: refuse a faulty configuration, write the token files, serve on loopback."""

from __future__ import annotations

import asyncio
import json
import socket
import stat
import threading
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from tests.support.sockets import socket_path
from zephyros.adapters.hermes.engine import HERMES_VERSION
from zephyros.adapters.opencode.engine import OPENCODE_VERSION
from zephyros.app import hub as program
from zephyros.app.cli import main as cli_main
from zephyros.app.hub import main, run
from zephyros.core.config import Config, parse_config
from zephyros.core.lifecycle import TurnEngine
from zephyros.core.ports.agent_engine import Side

DIGEST = "ghcr.io/example/app@sha256:" + "e" * 64


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def make_config(tmp_path: Path) -> Config:
    return parse_config(
        {
            "schema_version": "1.0.0",
            "images": {"instance": DIGEST, "guard": DIGEST},
            "hub": {
                "agent_port": free_port(),
                "operator_port": free_port(),
                "main_agent_port": free_port(),
                "egress_port": free_port(),
            },
            "paths": {
                "data_dir": str(tmp_path / "data"),
                "filter_socket": str(tmp_path / "filter.sock"),
                "operator_token_file": str(tmp_path / "tokens" / "operator.token"),
                "main_agent_token_file": str(tmp_path / "tokens" / "main-agent.token"),
                "control_socket": socket_path(tmp_path),
            },
        }
    )


def test_a_faulty_configuration_refuses_to_start_and_names_the_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "z.toml"
    path.write_text('schema_version = "1.0.0"\n[hub]\nagent_port = 1\n', encoding="utf-8")
    assert main(["--config", str(path)]) == 2
    error = capsys.readouterr().err
    assert "refused to start" in error
    assert "hub.agent_port" in error


def test_the_three_listeners_serve_on_loopback_and_stop_cleanly(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()
    thread = threading.Thread(
        target=lambda: loop.run_until_complete(run(config, agent_bind="127.0.0.1", stop=stop)),
        daemon=True,
    )
    thread.start()
    operator_url = f"http://127.0.0.1:{config.hub.operator_port}"
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                if httpx.get(f"{operator_url}/operator/health", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            assert time.monotonic() < deadline, "the hub did not start"
            time.sleep(0.05)
        token = Path(config.paths.operator_token_file).read_text(encoding="ascii").strip()
        called = httpx.post(
            f"{operator_url}/operator/call",
            json={"tool": "project.create", "arguments": {"name": "Served"}},
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        assert called.status_code == 200
        assert called.json()["data"]["project_id"].startswith("p_")
        main_token = Path(config.paths.main_agent_token_file).read_text(encoding="ascii").strip()
        main_answer = httpx.post(
            f"http://127.0.0.1:{config.hub.main_agent_port}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": f"Bearer {main_token}"},
            timeout=5,
        )
        assert main_answer.status_code == 200
        # The control socket is the hub user's alone, and rotates a token in the running hub.
        control = Path(config.paths.control_socket)
        assert stat.S_IMODE(control.stat().st_mode) == 0o600
        assert cli_main(["hub", "rotate-token", "operator", "--socket", str(control)]) == 0
        rotated = Path(config.paths.operator_token_file).read_text(encoding="ascii").strip()
        assert rotated != token
        for secret, status in ((token, 401), (rotated, 200)):
            answer = httpx.post(
                f"{operator_url}/operator/call",
                json={"tool": "whoami", "arguments": {}},
                headers={"Authorization": f"Bearer {secret}"},
                timeout=5,
            )
            assert answer.status_code == status
        listing = httpx.get(f"http://127.0.0.1:{config.hub.agent_port}/v1/models", timeout=5)
        assert listing.status_code == 200
        assert "server" not in listing.headers
        # The egress proxy of the default level (regulated, ADR 0024) listens beside the agent
        # endpoint, and opens nothing without a project token.
        with socket.create_connection(("127.0.0.1", config.hub.egress_port), timeout=5) as proxy:
            proxy.sendall(b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org\r\n\r\n")
            assert proxy.recv(64).startswith(b"HTTP/1.1 407")
    finally:
        loop.call_soon_threadsafe(stop.set)
        thread.join(timeout=15)
    assert not thread.is_alive()
    with pytest.raises(httpx.TransportError):
        httpx.get(f"{operator_url}/operator/health", timeout=1)
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", config.hub.egress_port), timeout=1).close()


@pytest.mark.parametrize("level", ["sealed", "open"])
def test_no_egress_proxy_listens_outside_the_regulated_level(tmp_path: Path, level: str) -> None:
    config = make_config(tmp_path)
    config = replace(config, network=replace(config.network, egress=level))
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()
    thread = threading.Thread(
        target=lambda: loop.run_until_complete(run(config, agent_bind="127.0.0.1", stop=stop)),
        daemon=True,
    )
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                url = f"http://127.0.0.1:{config.hub.agent_port}/v1/models"
                if httpx.get(url, timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            assert time.monotonic() < deadline, "the hub did not start"
            time.sleep(0.05)
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", config.hub.egress_port), timeout=1).close()
    finally:
        loop.call_soon_threadsafe(stop.set)
        thread.join(timeout=15)


def test_an_egress_port_already_taken_stops_the_hub_with_an_error(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", config.hub.egress_port))
        taken.listen()
        with pytest.raises(OSError, match="address already in use"):
            asyncio.run(run(config, agent_bind="127.0.0.1", stop=asyncio.Event()))


def test_a_hub_that_cannot_keep_its_channels_off_the_instances_does_not_start(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "core" when every address of the hub is in the pool (or the hub is not in a Linux
    container) refuses to start before anything opens (ADR 0025)."""
    config = make_config(tmp_path)
    config = replace(
        config,
        network=replace(config.network, instance_pool="0.0.0.0/0"),
        hub=replace(config.hub, operator_bind="core"),
    )
    monkeypatch.setattr(program, "load_config", lambda _path: config)
    assert main(["--config", str(tmp_path / "z.toml")]) == 2
    assert "refused to start" in capsys.readouterr().err
    assert not Path(config.paths.data_dir).exists()


# ----------------------------------------------------------------------------- ADR 0029
def test_the_turns_record_the_engine_each_side_runs_with_its_pinned_version(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    hermes = TurnEngine("hermes", HERMES_VERSION)
    assert program.turn_engines(config) == {Side.OWNER: hermes, Side.CONTRACTOR: hermes}
    config = replace(
        config,
        images=replace(config.images, opencode=DIGEST),
        instances=replace(config.instances, contractor_engine="opencode"),
    )
    assert program.turn_engines(config) == {
        Side.OWNER: hermes,
        Side.CONTRACTOR: TurnEngine("opencode", OPENCODE_VERSION),
    }


# ----------------------------------------------------------------------------- ADR 0030
def tools_of(tmp_path: Path) -> Path:
    root = tmp_path / "tools"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "sim").write_bytes(b"#!/bin/sh\necho ok\n")
    root.chmod(0o755)
    (root / "bin").chmod(0o755)
    (root / "bin" / "sim").chmod(0o755)
    return root


def test_a_hub_whose_tools_an_instance_could_change_does_not_start(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tools_of(tmp_path)
    (root / "bin" / "sim").chmod(0o777)
    config = make_config(tmp_path)
    config = replace(config, tools=replace(config.tools, source=str(root)))
    monkeypatch.setattr(program, "load_config", lambda _path: config)
    assert main(["--config", str(tmp_path / "z.toml")]) == 2
    error = capsys.readouterr().err
    assert "refused to start: the tools directory cannot be mounted:" in error
    assert "'bin/sim' is writable by every user" in error
    assert not Path(config.paths.data_dir).exists()


def test_the_hub_logs_the_fingerprint_of_the_tools_when_it_starts(tmp_path: Path) -> None:
    from zephyros.adapters.project_tools import ToolsDirectory

    root = tools_of(tmp_path)
    config = make_config(tmp_path)
    config = replace(config, tools=replace(config.tools, source=str(root)))
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()
    thread = threading.Thread(
        target=lambda: loop.run_until_complete(run(config, agent_bind="127.0.0.1", stop=stop)),
        daemon=True,
    )
    thread.start()
    operator_url = f"http://127.0.0.1:{config.hub.operator_port}"
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                if httpx.get(f"{operator_url}/operator/health", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            assert time.monotonic() < deadline, "the hub did not start"
            time.sleep(0.05)
        token = Path(config.paths.operator_token_file).read_text(encoding="ascii").strip()
        answer = httpx.post(
            f"{operator_url}/operator/call",
            json={"tool": "log.read", "arguments": {"stream": "hub", "limit": 100}},
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        records = [json.loads(line) for line in answer.json()["data"]["records"]]
    finally:
        loop.call_soon_threadsafe(stop.set)
        thread.join(timeout=15)
    checks = [r for r in records if r["action"] == "tools.check"]
    assert len(checks) == 1
    measured = ToolsDirectory(root).digest()
    assert checks[0]["outcome"] == "ok"
    assert checks[0]["parameters"] == {
        "source": str(root),
        "target": "/opt/project-tools",
        "tools": measured.fingerprint,
        "files": 1,
        "bytes": measured.bytes,
    }
