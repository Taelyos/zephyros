"""The reaper, on the real runtime: a turn does not outlive the hub (build log, decision 22).

A whole Zephyros runs with a short turn limit. A turn is started, the hub is stopped (the
case the reaper exists for), and the filter must stop and remove the turn on its own. The
guard of the instance is left running, and the suite would fail loudly if the turn outlived
its limit.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest

from tests.runtime.deployment import Deployment
from tests.runtime.rig import docker
from zephyros.core.containers import guard_name, turn_name
from zephyros.core.ports.agent_engine import Side

pytestmark = [pytest.mark.runtime, pytest.mark.acceptance, pytest.mark.slow]

CONTRACTOR = Side.CONTRACTOR
TURN_LIMIT = 60
"""Also written into the configuration. The reaper sweeps every 30 s and waits a three-poll
grace (15 s at the default 5-second poll) so a live hub decides first; the reap happens
between 75 s and 105 s of turn life, well before the terminal tool's own 180-second default,
which is why the turn is still running when the reap happens."""


@pytest.fixture(scope="module")
def deployment(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Deployment]:
    made = Deployment(
        tmp_path_factory.mktemp("reaper"),
        pool="10.216.0.0/16",
        extra_config=f"[limits]\nturn_seconds = {TURN_LIMIT}\n",
    )
    made.up()
    try:
        yield made
    finally:
        made.down()


def _running(name: str) -> bool:
    done = docker("inspect", "-f", "{{.State.Running}}", name, check=False)
    return done.returncode == 0 and done.stdout.strip() == "true"


def _wait_for(check: object, seconds: float, what: str) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():  # type: ignore[operator]  # reason: a callable in the tests
            return
        time.sleep(2)
    raise AssertionError(what)


def test_a_turn_is_stopped_and_removed_when_the_hub_is_gone(deployment: Deployment) -> None:
    project = deployment.project()
    deployment.data(
        "instance.start", project_id=project, side=CONTRACTOR.value, prompt="RUN sleep 600"
    )
    turn = turn_name(project, CONTRACTOR)
    guard = guard_name(project, CONTRACTOR)
    _wait_for(lambda: _running(turn), 60, "the turn never started")

    # The hub dies a hard death. `docker stop` would not do: a hub that stops gracefully ends
    # its own turns (core/lifecycle.py, shutdown), and the reaper would prove nothing.
    docker("update", "--restart=no", deployment.hub)
    docker("kill", deployment.hub)
    assert not _running(deployment.hub)
    assert _running(turn), "the turn must survive the death of the hub"

    # The filter's reaper stops and removes the turn within one sweep of the limit.
    _wait_for(
        lambda: not _running(turn),
        TURN_LIMIT + 60,
        "the reaper never stopped the turn",
    )
    assert docker("inspect", turn, check=False).returncode != 0  # removed, not only stopped
    # The guard, which is not a turn, is left alone.
    assert _running(guard)
