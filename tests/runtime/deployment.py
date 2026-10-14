"""A whole Zephyros on the runtime, for the tests: scripts/deploy.sh with names of its own.

The deployment uses its own compose project, hub container name, ports and address pool, so it
can run next to another Zephyros on the same machine. Tools are called on the operator channel
from inside the hub container, the way `scripts/deploy.sh call` does. What a turn did is read
from the session database of Hermes in the instance's home volume, read-only.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from tests.runtime.rig import ROOT, docker
from zephyros.core.containers import VolumeKind, guard_name, network_name, turn_name, volume_name
from zephyros.core.ports.agent_engine import Side

SESSIONS = """
import json, sqlite3, sys
db = sqlite3.connect('file:/opt/data/state.db?mode=ro', uri=True)
rows = db.execute('select role, tool_name, content from messages order by id').fetchall()
print(json.dumps([{'role': r, 'tool': t, 'content': c} for r, t, c in rows]))
"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Deployment:
    def __init__(
        self,
        state: Path,
        pool: str = "10.215.0.0/16",
        *,
        extra_config: str = "",
        env_extra: dict[str, str] | None = None,
    ) -> None:
        suffix = secrets.token_hex(3)
        self.name = f"zsrt{suffix}"
        self.hub = f"{self.name}-hub"
        self.projects: list[str] = []
        self.env = {
            **os.environ,
            "ZEPHYROS_PROJECT": self.name,
            "ZEPHYROS_HUB_NAME": self.hub,
            "ZEPHYROS_OPERATOR_PORT": str(free_port()),
            "ZEPHYROS_MAIN_AGENT_PORT": str(free_port()),
            "ZEPHYROS_POOL": pool,
            "ZEPHYROS_STATE": str(state),
            **(env_extra or {}),
        }
        if extra_config:
            self.env["ZEPHYROS_EXTRA_CONFIG"] = extra_config

    def script(self, *args: str, timeout: float = 1800) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603  # reason: the repository's own script
            [str(ROOT / "scripts" / "deploy.sh"), *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )

    def up(self) -> None:
        self.script("up")

    def down(self) -> None:
        for project in self.projects:
            for side in Side:
                for name in (turn_name(project, side), guard_name(project, side)):
                    docker("rm", "-f", name, check=False)
                docker(
                    "network",
                    "disconnect",
                    "-f",
                    network_name(project, side),
                    self.hub,
                    check=False,
                )
                docker("network", "rm", network_name(project, side), check=False)
                for kind in VolumeKind:
                    docker("volume", "rm", volume_name(project, side, kind), check=False)
        self.script("down", "--volumes", timeout=300)

    def _wait_hub_healthy(self, seconds: float = 120) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            state = docker(
                "inspect", "-f", "{{.State.Health.Status}}", self.hub, check=False
            ).stdout.strip()
            if state == "healthy":
                return
            time.sleep(0.5)
        raise AssertionError("the hub did not become healthy after the restart")

    def restart_hub(self) -> None:
        """Restart the hub, the way the attacks of the threat model repeat after a hub restart."""
        docker("restart", self.hub)
        self._wait_hub_healthy()

    # ------------------------------------------------------------------ the operator channel
    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        done = docker(
            "exec",
            self.hub,
            "/opt/zephyros/bin/zephyros",
            "hub",
            "call",
            tool,
            "--args",
            json.dumps(arguments),
            "--url",
            "http://127.0.0.1:8081",
            check=False,
        )
        answer: dict[str, Any] = json.loads(done.stdout)
        return answer

    def data(self, tool: str, **arguments: Any) -> dict[str, Any]:
        answer = self.call(tool, **arguments)
        assert answer.get("ok") is True, answer
        data: dict[str, Any] = answer["data"]
        return data

    def project(self, operations: list[dict[str, Any]] | None = None) -> str:
        project_id = str(self.data("project.create", name="Runtime test")["project_id"])
        self.projects.append(project_id)
        if operations:
            self.data(
                "contract.apply_reserved",
                project_id=project_id,
                summary="the contract of the test",
                operations=operations,
            )
        return project_id

    def fingerprint(self, project_id: str) -> str:
        """The contract as the hub holds it (oracle O-STATE)."""
        contract = self.data("contract.read", project_id=project_id)
        return hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()

    def records(self, project_id: str) -> list[dict[str, Any]]:
        lines = self.data("log.read", stream=project_id, limit=500)["records"]
        return [json.loads(line) for line in lines]

    # ------------------------------------------------------------------ turns
    def turn(self, project_id: str, side: Side, prompt: str, seconds: float = 240) -> str:
        """Start a turn and wait for its end. Returns how it ended."""
        self.data("instance.start", project_id=project_id, side=side.value, prompt=prompt)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            status = self.data("instance.status", project_id=project_id, side=side.value)
            if status["state"] in ("idle", "absent"):
                return str(status["last_turn"])
            time.sleep(2)
        raise AssertionError(f"the turn of {project_id} did not end in {seconds} s")

    def messages(self, project_id: str, side: Side) -> list[dict[str, Any]]:
        """Every message of the instance's Hermes session, tool results included."""
        image = self.instance_image()
        done = docker(
            "run",
            "--rm",
            "--user",
            "10000:10000",
            "--read-only",
            "--network",
            "none",
            "-v",
            f"{volume_name(project_id, side, VolumeKind.HOME)}:/opt/data:ro",
            "--entrypoint",
            "/opt/hermes/.venv/bin/python",
            image,
            "-c",
            SESSIONS,
        )
        found: list[dict[str, Any]] = json.loads(done.stdout)
        return found

    def tool_results(self, project_id: str, side: Side) -> list[str]:
        return [m["content"] or "" for m in self.messages(project_id, side) if m["role"] == "tool"]

    def work_file(self, project_id: str, side: Side, path: str) -> str | None:
        done = docker(
            "run",
            "--rm",
            "--user",
            "10000:10000",
            "--read-only",
            "--network",
            "none",
            "-v",
            f"{volume_name(project_id, side, VolumeKind.WORK)}:/work:ro",
            "--entrypoint",
            "cat",
            self.instance_image(),
            f"/work/{path}",
            check=False,
        )
        return done.stdout if done.returncode == 0 else None

    def instance_image(self) -> str:
        return docker("image", "inspect", "-f", "{{.Id}}", "zephyros-instance:local").stdout.strip()
