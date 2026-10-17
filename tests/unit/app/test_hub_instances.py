"""The hub program with its instances: operator channel, worker thread, filter on a socket, runtime.

Everything is real except the container runtime behind the filter.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.support.fake_docker import FakeDocker
from tests.support.filter_process import Running
from tests.support.sockets import free_port, socket_path
from zephyros.adapters.filter.journal import MemoryJournal
from zephyros.app.hub import run
from zephyros.core.config import Config, parse_config

HERMES_TAIL = ["--oneshot", "--continue=zephyros", "--create-if-missing", "--quiet"]

DIGEST = "ghcr.io/example/app@sha256:" + "e" * 64


@pytest.fixture
def base() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="zh"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def make_config(base: Path) -> Config:
    return parse_config(
        {
            "schema_version": "1.1.0",
            "images": {"instance": DIGEST, "guard": DIGEST},
            "hub": {
                "agent_port": free_port(),
                "operator_port": free_port(),
                "main_agent_port": free_port(),
                "egress_port": free_port(),
            },
            "runtime": {"poll_seconds": 1, "guard_health_seconds": 10},
            "paths": {
                "data_dir": str(base / "data"),
                "filter_socket": str(base / "f.sock"),
                "operator_token_file": str(base / "t" / "operator.token"),
                "main_agent_token_file": str(base / "t" / "main-agent.token"),
                "control_socket": socket_path(base),
            },
        }
    )


class Hub:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._loop = asyncio.new_event_loop()
        self._stop = asyncio.Event()
        self._failure: list[BaseException] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{config.hub.operator_port}"
        self.wait_until(self._healthy, "the hub did not start")
        self.token = Path(config.paths.operator_token_file).read_text(encoding="ascii").strip()

    def _serve(self) -> None:
        try:
            self._loop.run_until_complete(run(self.config, agent_bind="127.0.0.1", stop=self._stop))
        except BaseException as error:  # reason: reported by the test that started it
            self._failure.append(error)

    def _healthy(self) -> bool:
        try:
            return httpx.get(f"{self.url}/operator/health", timeout=1).status_code == 200
        except httpx.TransportError:
            return False

    @staticmethod
    def wait_until(check: Callable[[], bool], message: str, seconds: float = 20) -> None:
        deadline = time.monotonic() + seconds
        while not check():
            assert time.monotonic() < deadline, message
            time.sleep(0.05)

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        answer = httpx.post(
            f"{self.url}/operator/call",
            json={"tool": tool, "arguments": arguments},
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=10,
        )
        body: dict[str, Any] = answer.json()
        return body

    def state(self, project: str, side: str) -> str:
        data = self.call("instance.status", project_id=project, side=side)["data"]
        return str(data["state"])

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=20)
        assert not self._thread.is_alive()
        assert not self._failure, self._failure


@pytest.fixture
def stack(base: Path) -> Iterator[tuple[Hub, FakeDocker]]:
    config = make_config(base)
    docker = FakeDocker()
    docker.add_hub()
    filter_ = Running(config, docker, MemoryJournal())
    hub = Hub(config)
    try:
        yield hub, docker
    finally:
        hub.close()
        filter_.close()


def project_of(hub: Hub) -> str:
    return str(hub.call("project.create", name="Served")["data"]["project_id"])


def test_an_instance_is_started_watched_and_stopped_from_the_operator_channel(
    stack: tuple[Hub, FakeDocker],
) -> None:
    hub, docker = stack
    project = project_of(hub)
    hub.wait_until(
        lambda: (
            hub.call("instance.status", project_id=project, side="contractor")["ok"] is True
            and hub.call("instance.start", project_id=project, side="contractor", prompt="Go.")[
                "ok"
            ]
            is True
        ),
        "the hub never took stock of its instances",
    )
    hub.wait_until(lambda: hub.state(project, "contractor") == "turn", "the turn did not start")
    turn = f"zs-turn-{project}-contractor"
    assert docker.containers[turn]["Config"]["Cmd"] == ["chat", "--query=Go.", *HERMES_TAIL]
    busy = hub.call("instance.start", project_id=project, side="contractor", prompt="Again.")
    assert busy["ok"] is False
    docker.finish(turn, 0)
    hub.wait_until(lambda: hub.state(project, "contractor") == "idle", "the end was not noticed")
    status = hub.call("instance.status", project_id=project, side="contractor")["data"]
    assert status["last_turn"] == "completed"
    hub.call("instance.stop", project_id=project, side="contractor")
    hub.wait_until(lambda: hub.state(project, "contractor") == "absent", "the stop did not finish")
    assert not [name for name in docker.containers if name.startswith("zs-")]


def test_the_log_of_the_project_tells_the_lifecycle(stack: tuple[Hub, FakeDocker]) -> None:
    hub, _docker = stack
    project = project_of(hub)
    hub.wait_until(
        lambda: (
            hub.call("instance.start", project_id=project, side="owner", prompt="Go.")["ok"] is True
        ),
        "the hub never took stock of its instances",
    )
    hub.wait_until(lambda: hub.state(project, "owner") == "turn", "the turn did not start")
    hub.call("instance.stop", project_id=project, side="owner")
    hub.wait_until(lambda: hub.state(project, "owner") == "absent", "the stop did not finish")
    lines = hub.call("log.read", stream=project)["data"]["records"]
    text = "\n".join(str(line) for line in lines)
    for expected in (
        "instance.start",
        "token.issue",
        "turn.start",
        "instance.stop",
        "token.revoke",
    ):
        assert expected in text
    assert hub.call("log.verify", stream=project)["data"]["ok"] is True


def test_a_hub_that_stops_ends_its_turns(base: Path) -> None:
    config = make_config(base)
    docker = FakeDocker()
    docker.add_hub()
    filter_ = Running(config, docker, MemoryJournal())
    hub = Hub(config)
    try:
        project = project_of(hub)
        hub.wait_until(
            lambda: (
                hub.call("instance.start", project_id=project, side="owner", prompt="Go.")["ok"]
                is True
            ),
            "the hub never took stock of its instances",
        )
        hub.wait_until(lambda: hub.state(project, "owner") == "turn", "the turn did not start")
    finally:
        hub.close()
    try:
        assert f"zs-turn-{project}-owner" not in docker.containers
        assert f"zs-guard-{project}-owner" in docker.containers  # the guard stays up
    finally:
        filter_.close()


def test_without_a_filter_the_hub_says_so_and_still_serves(base: Path) -> None:
    hub = Hub(make_config(base))
    try:
        project = project_of(hub)
        refused = hub.call("instance.start", project_id=project, side="owner", prompt="Go.")
        assert refused["ok"] is False
        assert hub.state(project, "owner") == "absent"
    finally:
        hub.close()
