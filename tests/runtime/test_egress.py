"""ATK-83 / EXG-48 on the real organs: the hub reaches the hosts the configuration names, and
nothing else. No telemetry, no hidden update check.

A network observer captures everything the hub sends (a capture container sharing its network
namespace). The notification receiver is the one configured host: a full delivery runs and the
decision notification reaches it — the positive control. The receiver answers with a redirect
to an unlisted host: the hub refuses and logs, and the capture proves nothing was sent to that
host. The unlisted listener records no peer of its own.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import pytest

from tests.runtime.deployment import Deployment
from tests.runtime.test_delivery_loop_real import (  # reason: the same bench
    BASE,
    REPOSITORY,
    SUB_TEST,
    _delivery_id,
    _wait_for_decision,
    _wait_for_staging,
    declare_prompt,
    delivery_prompt,
    requirements,
)
from tests.support.git_fixtures import make_forge_repository
from zephyros.core.ports.agent_engine import Side

pytestmark = [pytest.mark.runtime, pytest.mark.acceptance, pytest.mark.slow]

CONTRACTOR = Side.CONTRACTOR
OBSERVER = "zephyros-test-observer:test"
SECRET = "effect-egress-shared-secret-16"  # pragma: allowlist secret


def _docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(  # noqa: S603  # reason: the bench drives the real daemon
        ["docker", *args],  # noqa: S607  # reason: docker comes from PATH, as the deploy does
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed: {result.stderr[:400]}")
    return result.stdout


def _run_sidecar(name: str, *args: str) -> str:
    """Start a listener or a capture; return its container id."""
    out = _docker("run", "-d", "--rm", "--name", name, *args)
    return out.strip()


def _logs(name: str) -> str:
    return _docker("logs", name, check=False)


def _wait_for_line(name: str, needle: str, seconds: float = 60) -> str:
    deadline = time.monotonic() + seconds
    seen = ""
    while time.monotonic() < deadline:
        seen = _logs(name)
        if needle in seen:
            return seen
        time.sleep(2)
    raise AssertionError(f"{needle!r} never appeared in {name}'s log; saw: {seen[:600]}")


def test_the_hub_reaches_the_configured_host_only(tmp_path: Path) -> None:
    _docker("build", "-q", "-t", OBSERVER, str(Path(__file__).parent / "images" / "observer"))
    forge = tmp_path / "forge"
    forge.mkdir()
    make_forge_repository(forge, REPOSITORY, {"main": BASE})
    deployment = Deployment(
        tmp_path / "state",
        pool="10.219.0.0/16",
        extra_config=(f'[notifications]\nurl = "http://observer:8090/hook"\nsecret = "{SECRET}"\n'),
        env_extra={
            "ZEPHYROS_KIT": "tests",
            "ZEPHYROS_FORGE": "directory",
            "ZEPHYROS_FORGE_DIR": str(forge),
        },
    )
    deployment.up()
    suffix = deployment.name
    network = f"{deployment.name}_core"
    try:
        # The configured host: the receiver, which answers every POST with a redirect to a host
        # the configuration does not name.
        _run_sidecar(
            f"{suffix}-trap",
            "--network",
            network,
            "--network-alias",
            "observer",
            "-e",
            "REDIRECT_TO=http://elsewhere:8091/steal",
            OBSERVER,
            "tcp:8090",
        )
        # The unlisted host the redirect points at: it must never hear from the hub.
        _run_sidecar(
            f"{suffix}-elsewhere",
            "--network",
            network,
            "--network-alias",
            "elsewhere",
            OBSERVER,
            "tcp:8091",
        )
        # The network observer: a capture sharing the hub's network namespace.
        _run_sidecar(
            f"{suffix}-capture",
            "--net",
            f"container:{deployment.hub}",
            "--cap-add",
            "NET_RAW",
            "--cap-add",
            "NET_ADMIN",
            "--entrypoint",
            "tcpdump",
            OBSERVER,
            "-l",
            "-n",
            "-q",
            "-i",
            "any",
        )
        elsewhere_ip = _docker(
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            f"{suffix}-elsewhere",
        ).strip()
        assert elsewhere_ip

        # A full delivery runs: the decision notification reaches the configured host.
        project = deployment.project(requirements())
        deployment.data("project.prepare", project_id=project, repository=REPOSITORY, branch="main")
        deployment.data("acceptance.put", project_id=project, path="test_sub.py", content=SUB_TEST)
        _wait_for_staging(deployment, project)
        ended = deployment.turn(project, CONTRACTOR, delivery_prompt(), seconds=420)
        assert ended == "completed", ended
        delivery_id = _delivery_id(deployment, project)
        deployment.turn(project, CONTRACTOR, declare_prompt(delivery_id), seconds=180)
        found = _wait_for_decision(deployment, project, delivery_id, seconds=240)
        assert found["state"] == "proposed", (
            f"state={found['state']} detail={str(found.get('detail'))[:800]} "
            f"records={deployment.records(project)[-5:]}"
        )

        # Positive control: the configured host was reached — it saw the delivery.
        _wait_for_line(f"{suffix}-trap", "TCP ", seconds=120)
        # The redirect was refused and logged, with the host the configuration does not name.
        records = deployment.records(project)
        refusals = [r for r in records if r.get("action") == "egress.refused"]
        assert refusals, records[-6:]
        assert refusals[0]["parameters"]["host"] == "elsewhere"
        notifications = [r for r in records if r.get("action") == "notification.sent"]
        assert notifications
        assert notifications[-1]["outcome"] == "error"

        # The unlisted host heard nothing, and the capture shows no packet ever went to it.
        time.sleep(5)
        assert "TCP " not in _logs(f"{suffix}-elsewhere")
        assert elsewhere_ip not in _logs(f"{suffix}-elsewhere")
        capture = _logs(f"{suffix}-capture")
        assert capture.strip(), "the observer captured nothing at all"
        # A gratuitous ARP of the host itself (`who-has X tell X`) is the host announcing its
        # own address to the segment — the segment hears it; the hub does not reach it. What
        # must be absent: a packet addressed to the host, anyone looking for it, or the host
        # answering a lookup. The hub's own multicast noise names no one of the sort.
        toward_elsewhere = [
            line
            for line in capture.splitlines()
            if (
                re.search(rf">\s*{re.escape(elsewhere_ip)}[.:]", line)
                or (
                    f"who-has {elsewhere_ip} " in line
                    and f"tell {elsewhere_ip}," not in line
                    and "tell 0.0.0.0," not in line
                )
                or f"ARP, Reply {elsewhere_ip} " in line
            )
        ]
        assert not toward_elsewhere, toward_elsewhere[:3]
        assert f"{elsewhere_ip}.8091" not in capture  # never even dialled
        assert ".8091:" not in capture
        # No hidden update check, no telemetry: everything the hub sent went to the configured
        # host or to the resolver — nothing else left the container.
        networks = json.loads(
            _docker("inspect", "-f", "{{json .NetworkSettings.Networks}}", deployment.hub)
        )
        hub_ip = next(
            entry["IPAddress"] for name, entry in networks.items() if name.endswith("core")
        )
        assert hub_ip
        trap_networks = json.loads(
            _docker("inspect", "-f", "{{json .NetworkSettings.Networks}}", f"{suffix}-trap")
        )
        trap_ip = next(entry["IPAddress"] for entry in trap_networks.values())
        assert trap_ip
        outbound = [line for line in capture.splitlines() if f" IP {hub_ip}." in line]
        assert outbound, "the capture saw no traffic from the hub at all"
        for line in outbound:
            # every packet from the hub goes to the configured host's own address, or to
            # Docker's embedded resolver — nothing else, address included
            assert f"{trap_ip}.8090" in line or "127.0.0.11.53" in line, line
    finally:
        hub_logs = _logs(deployment.hub)
        if "Traceback" in hub_logs:
            print("HUB TRACEBACK:\n" + hub_logs[-3000:])  # reason: the bench
        for name in (f"{suffix}-capture", f"{suffix}-trap", f"{suffix}-elsewhere"):
            _docker("rm", "-f", name, check=False)
        deployment.down()
