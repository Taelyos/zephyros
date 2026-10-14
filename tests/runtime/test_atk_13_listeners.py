"""ATK-13 without a guard: from an instance network, the operator channel and the main-agent
endpoint are closed by construction, and the agent endpoint answers (ADR 0025).

A whole Zephyros runs (scripts/deploy.sh). The test makes an instance network the way the filter
does (the template of `network_spec`: a bridge with a gateway, a subnet of the pool, a name that
sorts after the core network's), attaches the hub to it at its fixed address as the filter does,
and starts no guard: a probe without any capability joins that network directly. Whatever stops
it from reaching the two listeners is therefore not a guard's rule. From the machine, the two
published ports still answer, so the publication stayed on the core network after the hub joined
an instance network. Everything is checked again after a restart of the hub, which then starts
with the instance network already attached.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from tests.runtime.deployment import Deployment
from tests.runtime.rig import docker
from zephyros.core.containers import hub_address, network_name, network_spec
from zephyros.core.ports.agent_engine import Side

pytestmark = [pytest.mark.runtime, pytest.mark.acceptance, pytest.mark.slow]

POOL = "10.221.0.0/16"
SUBNET = ipaddress.IPv4Network("10.221.250.0/24")
AGENT, OPERATOR, MAIN_AGENT = 8080, 8081, 8082

PROBE = """
import json, socket, sys

found = {}
for name, host, port in json.loads(sys.argv[1]):
    try:
        socket.create_connection((host, port), timeout=3).close()
        found[name] = "open"
    except ConnectionRefusedError:
        found[name] = "refused"
    except TimeoutError:
        found[name] = "timeout"
    except OSError as error:
        found[name] = f"unreachable ({error.errno})"
print(json.dumps(found))
"""


@pytest.fixture(scope="module")
def deployment(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Deployment]:
    made = Deployment(tmp_path_factory.mktemp("atk13"), pool=POOL)
    made.up()
    try:
        yield made
    finally:
        made.down()


@pytest.fixture(scope="module")
def instance_network(deployment: Deployment) -> str:
    """An instance network with the hub on it and no guard; `down` removes it with its project."""
    project_id = deployment.project()
    spec = network_spec(project_id, Side.CONTRACTOR, SUBNET)
    ipam = spec["IPAM"]["Config"][0]
    labels = [f"--label={key}={value}" for key, value in spec["Labels"].items()]
    docker(
        "network",
        "create",
        "--driver",
        spec["Driver"],
        "--subnet",
        ipam["Subnet"],
        "--gateway",
        ipam["Gateway"],
        *labels,
        spec["Name"],
    )
    assert spec["Name"] == network_name(project_id, Side.CONTRACTOR)
    docker("network", "connect", "--ip", str(hub_address(SUBNET)), spec["Name"], deployment.hub)
    return str(spec["Name"])


def probe(
    deployment: Deployment, network: str, targets: list[tuple[str, str, int]]
) -> dict[str, str]:
    """Connect to each target from a container on `network`: no capability, no guard."""
    image = docker("image", "inspect", "-f", "{{.Id}}", "zephyros:local").stdout.strip()
    done = docker(
        "run",
        "--rm",
        "--name",
        f"{deployment.name}-atk13-probe",
        "--network",
        network,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "10000:10000",
        "--read-only",
        "--entrypoint",
        "/opt/zephyros/bin/python",
        image,
        "-c",
        PROBE,
        json.dumps(targets),
        timeout=120,
    )
    found: dict[str, str] = json.loads(done.stdout)
    return found


def core_address(deployment: Deployment) -> str:
    template = '{{(index .NetworkSettings.Networks "' + deployment.core_network + '").IPAddress}}'
    return docker("inspect", "-f", template, deployment.hub).stdout.strip()


def published(deployment: Deployment, name: str) -> int:
    return int(deployment.env[name])


def from_the_machine(deployment: Deployment) -> tuple[int, int]:
    """The status of the operator channel's health and of an unauthenticated main-agent call,
    through the ports published on the loopback interface of the machine."""
    health = urllib.request.Request(
        f"http://127.0.0.1:{published(deployment, 'ZEPHYROS_OPERATOR_PORT')}/operator/health",
        # The Host the command line sends on the default port, which the channel serves.
        headers={"Host": f"127.0.0.1:{OPERATOR}"},
    )
    mcp = urllib.request.Request(
        f"http://127.0.0.1:{published(deployment, 'ZEPHYROS_MAIN_AGENT_PORT')}/mcp",
        data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    statuses: list[int] = []
    for request in (health, mcp):
        try:
            # A fixed http URL on the loopback.
            with urllib.request.urlopen(request, timeout=5) as answer:  # noqa: S310  # reason: above
                statuses.append(answer.status)
        except urllib.error.HTTPError as error:
            statuses.append(error.code)
    return statuses[0], statuses[1]


def check_the_listeners(deployment: Deployment, network: str) -> None:
    on_instance = str(hub_address(SUBNET))
    core = core_address(deployment)
    gateway = str(SUBNET.network_address + 1)
    found = probe(
        deployment,
        network,
        [
            ("agent endpoint", on_instance, AGENT),
            ("operator channel", on_instance, OPERATOR),
            ("main-agent endpoint", on_instance, MAIN_AGENT),
            ("operator channel, core address", core, OPERATOR),
            ("main-agent endpoint, core address", core, MAIN_AGENT),
            ("published operator port", gateway, published(deployment, "ZEPHYROS_OPERATOR_PORT")),
            (
                "published main-agent port",
                gateway,
                published(deployment, "ZEPHYROS_MAIN_AGENT_PORT"),
            ),
        ],
    )
    # The positive control: the network works, and the hub is on it.
    assert found["agent endpoint"] == "open", found
    # Nothing listens on the hub's address of the instance network: the kernel refuses.
    assert found["operator channel"] == "refused", found
    assert found["main-agent endpoint"] == "refused", found
    # The rest is not on this network at all.
    for name in (
        "operator channel, core address",
        "main-agent endpoint, core address",
        "published operator port",
        "published main-agent port",
    ):
        assert found[name] != "open", found
    # And the real use is intact: the machine reaches both through their published ports.
    assert from_the_machine(deployment) == (200, 401)


def test_atk_13_no_guard_the_two_listeners_are_closed_on_an_instance_network(
    deployment: Deployment, instance_network: str
) -> None:
    check_the_listeners(deployment, instance_network)


def test_atk_13_no_guard_still_closed_after_a_restart_of_the_hub(
    deployment: Deployment, instance_network: str
) -> None:
    """The hub starts again attached to the instance network, and still keeps off it."""
    deployment.restart_hub()
    check_the_listeners(deployment, instance_network)
