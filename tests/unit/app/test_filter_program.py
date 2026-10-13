"""The filter program: a real Unix socket, the hub's client on one end, a fake runtime behind."""

from __future__ import annotations

import shutil
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.support.fake_docker import FakeDocker
from tests.support.filter_process import Running
from zephyros.adapters.filter.journal import MemoryJournal
from zephyros.app.filter import FilterStartError, bind_socket, main
from zephyros.core.broker import BrokerError
from zephyros.core.config import Config, parse_config
from zephyros.core.containers import Role, VolumeKind
from zephyros.core.ports.agent_engine import Side

DIGEST = "ghcr.io/example/app@sha256:" + "e" * 64
PROJECT = "p_abcdef12"
SUBNET = "10.200.0.16/28"
TOKEN = "zph_c_" + "T" * 43


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A directory with a short path: Unix socket paths are limited to about a hundred bytes."""
    path = Path(tempfile.mkdtemp(prefix="zf"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def make_config(base: Path) -> Config:
    return parse_config(
        {
            "schema_version": "1.0.0",
            "images": {"instance": DIGEST, "guard": DIGEST},
            "paths": {
                "data_dir": str(base / "data"),
                "filter_socket": str(base / "f.sock"),
                "operator_token_file": str(base / "t" / "operator.token"),
                "main_agent_token_file": str(base / "t" / "main-agent.token"),
            },
        }
    )


@pytest.fixture
def running(short_dir: Path) -> Iterator[tuple[Running, FakeDocker, MemoryJournal]]:
    docker = FakeDocker()
    docker.add_hub()
    journal = MemoryJournal()
    filter_ = Running(make_config(short_dir), docker, journal)
    try:
        yield filter_, docker, journal
    finally:
        filter_.close()


def test_the_hubs_client_drives_a_whole_instance_through_the_socket(
    running: tuple[Running, FakeDocker, MemoryJournal],
) -> None:
    filter_, docker, journal = running
    client = filter_.client()
    client.create_network(PROJECT, Side.CONTRACTOR, SUBNET)
    assert client.connect_hub(PROJECT, Side.CONTRACTOR) == "10.200.0.19"
    for kind in VolumeKind:
        client.create_volume(PROJECT, Side.CONTRACTOR, kind)
    client.start_guard(PROJECT, Side.CONTRACTOR, SUBNET)
    guard = client.inspect(Role.GUARD, PROJECT, Side.CONTRACTOR)
    assert guard is not None
    assert (guard.state, guard.health, guard.address) == ("running", "healthy", "10.200.0.18")
    assert client.inspect(Role.TURN, PROJECT, Side.CONTRACTOR) is None
    client.start_turn(PROJECT, Side.CONTRACTOR, TOKEN, "Do the work.")
    assert [(c.role, c.state) for c in client.list_containers()] == [
        ("guard", "running"),
        ("turn", "running"),
    ]
    docker.finish("zs-turn-p_abcdef12-contractor", 0)
    turn = client.inspect(Role.TURN, PROJECT, Side.CONTRACTOR)
    assert turn is not None
    assert (turn.state, turn.exit_code) == ("exited", 0)
    assert {e.action for e in client.events(0)} >= {"start", "die"}
    client.stop_turn(PROJECT, Side.CONTRACTOR)
    client.stop_guard(PROJECT, Side.CONTRACTOR)
    client.disconnect_hub(PROJECT, Side.CONTRACTOR)
    client.remove_network(PROJECT, Side.CONTRACTOR)
    for kind in VolumeKind:
        client.remove_volume(PROJECT, Side.CONTRACTOR, kind)
    assert not docker.networks
    assert not docker.volumes
    assert TOKEN not in str(journal.lines)
    client.close()


def test_a_refusal_comes_back_as_a_broker_error_with_its_code(
    running: tuple[Running, FakeDocker, MemoryJournal],
) -> None:
    filter_, _docker, _journal = running
    client = filter_.client()
    with pytest.raises(BrokerError) as caught:
        client.start_guard(PROJECT, Side.OWNER, SUBNET)
    assert caught.value.code == "not_found"
    with pytest.raises(BrokerError) as caught:
        client.create_network(PROJECT, Side.OWNER, "172.17.0.0/28")
    assert caught.value.code == "policy"
    client.close()


def test_the_socket_is_for_its_owner_and_group_only(
    running: tuple[Running, FakeDocker, MemoryJournal],
) -> None:
    filter_, _docker, _journal = running
    assert stat.S_IMODE(filter_.socket.stat().st_mode) == 0o660


def test_the_socket_is_removed_when_the_filter_stops(short_dir: Path) -> None:
    filter_ = Running(make_config(short_dir), FakeDocker(), MemoryJournal())
    socket_path = filter_.socket
    filter_.close()
    assert not socket_path.exists()


def test_a_stale_socket_is_replaced_and_anything_else_is_left_alone(short_dir: Path) -> None:
    path = short_dir / "f.sock"
    first = bind_socket(path)
    first.close()  # leaves the file, like a filter that was killed
    assert path.is_socket()
    second = bind_socket(path)
    assert path.is_socket()
    second.close()
    path.unlink()
    path.write_text("not mine", encoding="ascii")
    with pytest.raises(FilterStartError, match="not a socket"):
        bind_socket(path)
    assert path.read_text(encoding="ascii") == "not mine"
    path.unlink()
    path.symlink_to(short_dir / "elsewhere")
    with pytest.raises(FilterStartError):
        bind_socket(path)


def test_a_faulty_configuration_refuses_to_start(
    short_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = short_dir / "z.toml"
    path.write_text('schema_version = "1.0.0"\n[limits]\ncpus = 0\n', encoding="utf-8")
    assert main(["--config", str(path)]) == 2
    assert "refused to start" in capsys.readouterr().err
