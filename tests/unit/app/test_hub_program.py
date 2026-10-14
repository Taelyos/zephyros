"""The hub program: refuse a faulty configuration, write the token files, serve on loopback."""

from __future__ import annotations

import asyncio
import socket
import stat
import threading
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from zephyros.adapters.memory.instance_control import FakeInstanceControl
from zephyros.adapters.sqlite.database import Database
from zephyros.app.hub import (
    build_hub,
    ensure_token_file,
    main,
    prepare_tokens,
    run,
)
from zephyros.core.config import Config, parse_config
from zephyros.core.tokens import Endpoint

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


def test_a_token_file_is_written_readable_by_its_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "operator.token"
    ensure_token_file(path, "zph_h_" + "A" * 43, working=False)
    assert path.read_text(encoding="ascii").strip() == "zph_h_" + "A" * 43
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    ensure_token_file(path, "other", working=True)
    assert "other" not in path.read_text(encoding="ascii")
    ensure_token_file(path, None, working=False)
    assert "other" not in path.read_text(encoding="ascii")


def test_the_tokens_are_issued_once_and_reused(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    database = Database(":memory:")
    runtime = build_hub(database, FakeInstanceControl())
    hub, tokens = runtime.hub, runtime.tokens
    prepare_tokens(config, tokens, hub)
    operator_file = Path(config.paths.operator_token_file)
    main_file = Path(config.paths.main_agent_token_file)
    first = operator_file.read_text(encoding="ascii")
    assert first.startswith("zph_h_")
    assert main_file.read_text(encoding="ascii").startswith("zph_m_")
    prepare_tokens(config, tokens, hub)
    assert operator_file.read_text(encoding="ascii") == first
    operator_file.write_text("zph_h_" + "B" * 43 + "\n", encoding="ascii")  # a stale file
    prepare_tokens(config, tokens, hub)
    renewed = operator_file.read_text(encoding="ascii")
    assert renewed != first
    assert hub.authenticate(renewed.strip(), peer_address="172.18.0.1", endpoint=Endpoint.OPERATOR)
    database.close()


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
