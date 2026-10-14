"""The templates and the allow-list of the container filter (EXG-34, EXG-38; ATK-32, ATK-73)."""

from __future__ import annotations

import copy
import ipaddress
import string
from dataclasses import replace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from zephyros.adapters.hermes.engine import HermesEngine
from zephyros.core.containers import (
    HOME_PATH,
    INSTANCE_USER,
    LABEL_MANAGED,
    SANDBOX_LOG,
    WORK_PATH,
    Policy,
    Role,
    SpecError,
    VolumeKind,
    check_spec,
    guard_address,
    guard_name,
    guard_spec,
    hub_address,
    inside,
    instance_subnet,
    labels,
    network_name,
    network_spec,
    sandbox_spec,
    turn_name,
    turn_spec,
    volume_name,
    volume_spec,
)
from zephyros.core.ports.agent_engine import Side

PROJECT = "p_abcdef12"
DIGEST = "ghcr.io/example/app@sha256:"
POLICY = Policy(
    guard_image=DIGEST + "a" * 64,
    instance_image=DIGEST + "b" * 64,
    hub_container="zephyros-hub",
    hub_port=8080,
    model="zephyros-default",
    cpus=2.0,
    memory_mib=1024,
    pids=256,
    guard_memory_mib=32,
    network_pool="10.200.0.0/16",
    network_prefix=28,
)

HERMES_TAIL = ["--oneshot", "--continue=zephyros", "--create-if-missing", "--quiet"]
SUBNET = ipaddress.IPv4Network("10.200.0.16/28")
TOKEN = "zph_c_" + "A" * 43


def turn(**changes: str) -> dict[str, Any]:
    return turn_spec(
        POLICY,
        HermesEngine(),
        PROJECT,
        Side.CONTRACTOR,
        "10.200.0.19",
        changes.get("token", TOKEN),
        changes.get("prompt", "Do it."),
    )


def guard() -> dict[str, Any]:
    return guard_spec(POLICY, PROJECT, Side.CONTRACTOR, SUBNET, "10.200.0.19")


# ----------------------------------------------------------------------------- names and labels
def test_names_come_from_the_project_identifier_and_enumerations_only() -> None:
    assert network_name(PROJECT, Side.OWNER) == f"zs-net-{PROJECT}-owner"
    assert volume_name(PROJECT, Side.OWNER, VolumeKind.HOME) == f"zs-vol-{PROJECT}-owner-home"
    assert guard_name(PROJECT, Side.CONTRACTOR) == f"zs-guard-{PROJECT}-contractor"
    assert turn_name(PROJECT, Side.CONTRACTOR) == f"zs-turn-{PROJECT}-contractor"
    assert labels(Role.GUARD, PROJECT, Side.OWNER)[LABEL_MANAGED] == "true"


@pytest.mark.parametrize(
    "bad", ["", "p_x", "--privileged", "../..", "p_abcdef12; id", "P_ABCDEF12", "p_abcdef12\n"]
)
def test_a_name_is_never_built_from_something_that_is_not_a_project_identifier(bad: str) -> None:
    for build in (network_name, guard_name, turn_name):
        with pytest.raises(SpecError):
            build(bad, Side.OWNER)
    with pytest.raises(SpecError):
        volume_name(bad, Side.OWNER, VolumeKind.WORK)
    with pytest.raises(SpecError):
        labels(Role.NETWORK, bad, Side.OWNER)


# ----------------------------------------------------------------------------- subnets
def test_a_subnet_is_one_of_the_pool_of_the_configured_size() -> None:
    assert instance_subnet(POLICY, "10.200.7.32/28") == ipaddress.IPv4Network("10.200.7.32/28")
    for bad in (
        "10.201.0.0/28",
        "10.200.0.0/24",
        "10.200.0.1/28",
        "192.168.0.0/28",
        "fd00::/64",
        "nonsense",
        "10.200.0.0/16",
    ):
        with pytest.raises(SpecError):
            instance_subnet(POLICY, bad)


def test_the_guard_has_the_first_host_after_the_gateway_and_addresses_stay_inside() -> None:
    assert str(guard_address(SUBNET)) == "10.200.0.18"
    assert str(inside(SUBNET, "10.200.0.19")) == "10.200.0.19"
    assert str(hub_address(SUBNET)) == "10.200.0.19"
    assert hub_address(SUBNET) != guard_address(SUBNET)
    for bad in ("10.200.0.40", "172.17.0.2", "not-an-address"):
        with pytest.raises(SpecError):
            inside(SUBNET, bad)


# ----------------------------------------------------------------------------- templates
def test_the_network_template_is_a_small_bridge_with_the_given_subnet() -> None:
    spec = network_spec(PROJECT, Side.OWNER, SUBNET)
    assert spec["Name"] == f"zs-net-{PROJECT}-owner"
    assert spec["Internal"] is False
    assert spec["EnableIPv6"] is False
    assert spec["IPAM"]["Config"] == [{"Subnet": "10.200.0.16/28", "Gateway": "10.200.0.17"}]
    assert spec["Labels"][LABEL_MANAGED] == "true"


def test_the_volume_template_carries_the_labels() -> None:
    spec = volume_spec(PROJECT, Side.OWNER, VolumeKind.WORK)
    assert spec["Name"] == f"zs-vol-{PROJECT}-owner-work"
    assert spec["Labels"]["org.zephyros.volume"] == "work"
    assert spec["Driver"] == "local"


def test_the_guard_keeps_net_admin_only_and_has_the_rest_of_the_profile() -> None:
    spec = guard()
    host = spec["HostConfig"]
    assert host["CapDrop"] == ["ALL"]
    assert host["CapAdd"] == ["NET_ADMIN"]
    assert host["ReadonlyRootfs"] is True
    assert host["SecurityOpt"] == ["no-new-privileges"]
    assert host["Privileged"] is False
    assert host["IpcMode"] == "none"
    assert host["NetworkMode"] == f"zs-net-{PROJECT}-contractor"
    assert "Mounts" not in host
    assert (
        spec["NetworkingConfig"]["EndpointsConfig"][host["NetworkMode"]]["IPAMConfig"][
            "IPv4Address"
        ]
        == "10.200.0.18"
    )
    assert "HUB_IP=10.200.0.19" in spec["Env"]
    assert "HUB_PORT=8080" in spec["Env"]
    assert spec["Healthcheck"]["Test"][0] == "CMD"
    check_spec(spec, Role.GUARD, POLICY)


def test_a_turn_is_the_agent_engine_as_the_unprivileged_user_in_the_guards_namespace() -> None:
    spec = turn()
    host = spec["HostConfig"]
    assert spec["User"] == INSTANCE_USER
    assert spec["Cmd"] == ["chat", "--query=Do it.", *HERMES_TAIL]
    assert host["NetworkMode"] == f"container:zs-guard-{PROJECT}-contractor"
    assert [m["Target"] for m in host["Mounts"]] == [HOME_PATH, WORK_PATH]
    assert set(host["Tmpfs"]) == {"/tmp"}  # noqa: S108  # reason: a container path
    assert host["Memory"] == 1024 * 1024 * 1024
    assert host["PidsLimit"] == 256
    assert host["NanoCpus"] == 2_000_000_000
    assert host["CapDrop"] == ["ALL"]
    assert "CapAdd" not in host
    # the project token is the one secret
    assert [e for e in spec["Env"] if e.startswith("ZEPHYROS_TOKEN=")] == [
        f"ZEPHYROS_TOKEN={TOKEN}"
    ]
    assert "ZEPHYROS_HUB_URL=http://10.200.0.19:8080" in spec["Env"]
    # and no other variable of any kind travels with a turn (ATK-49): no git-hosting
    # identifier, no forge address, nothing but the engine's three and, at the default
    # level (regulated), the egress proxy of the hub (ADR 0024).
    assert {entry.split("=", 1)[0] for entry in spec["Env"]} == {
        "ZEPHYROS_HUB_URL",
        "ZEPHYROS_MODEL",
        "ZEPHYROS_TOKEN",
        *PROXY_VARIABLES,
    }
    check_spec(spec, Role.TURN, POLICY)


PROXY_VARIABLES = ("http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")


def test_a_regulated_turn_reaches_out_through_the_egress_proxy_of_the_hub_only() -> None:
    environment = dict(entry.split("=", 1) for entry in turn()["Env"])
    proxy = f"http://instance:{TOKEN}@10.200.0.19:8083"
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        assert environment[name] == proxy, name
    # the hub itself is called directly: the model and the tools never go through the proxy
    assert environment["no_proxy"] == environment["NO_PROXY"] == "10.200.0.19"


@pytest.mark.parametrize("level", ["sealed", "open"])
def test_a_sealed_or_open_turn_is_given_no_proxy(level: str) -> None:
    policy = replace(POLICY, egress=level)
    spec = turn_spec(
        policy, HermesEngine(), PROJECT, Side.CONTRACTOR, "10.200.0.19", TOKEN, "Do it."
    )
    assert {entry.split("=", 1)[0] for entry in spec["Env"]} == {
        "ZEPHYROS_HUB_URL",
        "ZEPHYROS_MODEL",
        "ZEPHYROS_TOKEN",
    }
    check_spec(spec, Role.TURN, policy)


@pytest.mark.parametrize("level", ["regulated", "sealed", "open"])
def test_the_guard_loads_the_egress_level_of_the_configuration(level: str) -> None:
    policy = replace(POLICY, egress=level, egress_port=8090)
    spec = guard_spec(policy, PROJECT, Side.CONTRACTOR, SUBNET, "10.200.0.19")
    assert f"EGRESS={level}" in spec["Env"]
    assert "PROXY_PORT=8090" in spec["Env"]
    check_spec(spec, Role.GUARD, policy)


def test_check_spec_refuses_a_guard_with_another_egress_level() -> None:
    spec = guard()
    spec["Env"] = [e if not e.startswith("EGRESS=") else "EGRESS=open" for e in spec["Env"]]
    with pytest.raises(SpecError, match="egress level"):
        check_spec(spec, Role.GUARD, POLICY)
    del spec["Env"]
    with pytest.raises(SpecError, match="egress level"):
        check_spec(spec, Role.GUARD, POLICY)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "zph_c_short",
        "zph_h_" + "A" * 43,
        "zph_m_" + "A" * 43,
        TOKEN + "x",
        "sk-live-key",
        "zph_c_" + "A" * 43 + "\n",
    ],
)
def test_a_turn_takes_a_project_token_and_nothing_else(token: str) -> None:
    with pytest.raises(SpecError, match="project token"):
        turn(token=token)


@pytest.mark.parametrize("prompt", ["", "x" * 4001, "nul\x00"])
def test_a_prompt_is_bounded(prompt: str) -> None:
    with pytest.raises(SpecError, match="prompt"):
        turn(prompt=prompt)


@pytest.mark.parametrize(
    "prompt", ["--privileged", "$(id)", "`id`", "a\nb", "-v /:/host", "; rm -rf /", "{{.Id}}"]
)
def test_a_prompt_is_one_argument_of_the_command_and_nothing_else(prompt: str) -> None:
    spec = turn(prompt=prompt)
    assert spec["Cmd"] == ["chat", f"--query={prompt}", *HERMES_TAIL]
    assert spec["HostConfig"]["CapDrop"] == ["ALL"]
    check_spec(spec, Role.TURN, POLICY)
    assert all(prompt not in str(value) for key, value in spec.items() if key != "Cmd")


# ----------------------------------------------------------------------------- the allow-list
def mutate(spec: dict[str, Any], path: str, value: object) -> dict[str, Any]:
    changed = copy.deepcopy(spec)
    node = changed
    *keys, last = path.split(".")
    for key in keys:
        node = node[key]
    if value is DELETE:
        del node[last]
    else:
        node[last] = value
    return changed


DELETE = object()
FORBIDDEN_MUTATIONS = {
    "privileged": ("HostConfig.Privileged", True),
    "cap add": ("HostConfig.CapAdd", ["SYS_ADMIN"]),
    "no cap drop": ("HostConfig.CapDrop", []),
    "cap drop missing": ("HostConfig.CapDrop", DELETE),
    "pid host": ("HostConfig.PidMode", "host"),
    "network host": ("HostConfig.NetworkMode", "host"),
    "network bridge": ("HostConfig.NetworkMode", "bridge"),
    "network of another guard": ("HostConfig.NetworkMode", "container:other"),
    "bind the root": ("HostConfig.Binds", ["/:/host"]),
    "bind the socket": ("HostConfig.Binds", ["/var/run/docker.sock:/var/run/docker.sock"]),
    "device": ("HostConfig.Devices", [{"PathOnHost": "/dev/sda"}]),
    "seccomp unconfined": ("HostConfig.SecurityOpt", ["seccomp=unconfined"]),
    "apparmor unconfined": ("HostConfig.SecurityOpt", ["no-new-privileges", "apparmor=unconfined"]),
    "new privileges": ("HostConfig.SecurityOpt", []),
    "userns host": ("HostConfig.UsernsMode", "host"),
    "writable root": ("HostConfig.ReadonlyRootfs", False),
    "ipc host": ("HostConfig.IpcMode", "host"),
    "ipc default": ("HostConfig.IpcMode", DELETE),
    "no memory limit": ("HostConfig.Memory", 0),
    "no pids limit": ("HostConfig.PidsLimit", DELETE),
    "no cpu limit": ("HostConfig.NanoCpus", 0),
    "port bindings": ("HostConfig.PortBindings", {"22/tcp": [{"HostPort": "22"}]}),
    "sysctls": ("HostConfig.Sysctls", {"net.ipv4.ip_forward": "1"}),
    "cgroup parent": ("HostConfig.CgroupParent", "/"),
    "volumes from": ("HostConfig.VolumesFrom", ["hub"]),
    "another image": ("Image", "alpine:latest"),
    "an image that is not pinned": ("Image", DIGEST + "c" * 64),
    "an entrypoint": ("Entrypoint", ["sh"]),
    "hostname": ("Hostname", "host"),
    "extra field": ("Domainname", "x"),
    "no labels": ("Labels", {}),
    "runtime socket in the environment": ("Env", ["DOCKER_HOST=unix:///var/run/docker.sock"]),
    "another name resolver": ("HostConfig.Dns", ["6.6.6.6"]),
}


@pytest.mark.parametrize("role", [Role.GUARD, Role.TURN])
@pytest.mark.parametrize("name", sorted(FORBIDDEN_MUTATIONS))
def test_check_spec_refuses_every_departure_from_the_profile(role: Role, name: str) -> None:
    path, value = FORBIDDEN_MUTATIONS[name]
    base = guard() if role is Role.GUARD else turn()
    if role is Role.GUARD and path == "HostConfig.CapAdd":
        pass  # a different capability is refused for the guard too
    with pytest.raises(SpecError):
        check_spec(mutate(base, path, value), role, POLICY)


def test_check_spec_refuses_a_volume_mounted_anywhere_but_the_two_paths_or_not_ours() -> None:
    for mount in (
        {"Type": "bind", "Source": "/", "Target": HOME_PATH},
        {"Type": "volume", "Source": "somebody-elses", "Target": HOME_PATH},
        {"Type": "volume", "Source": f"zs-vol-{PROJECT}-owner-home", "Target": "/etc"},
        {"Type": "tmpfs", "Target": "/mnt"},
    ):
        spec = turn()
        spec["HostConfig"]["Mounts"][0] = mount
        with pytest.raises(SpecError):
            check_spec(spec, Role.TURN, POLICY)
    spec = turn()
    spec["HostConfig"]["Mounts"].append(spec["HostConfig"]["Mounts"][0])
    with pytest.raises(SpecError):
        check_spec(spec, Role.TURN, POLICY)
    spec = guard()
    spec["HostConfig"]["Mounts"] = [turn()["HostConfig"]["Mounts"][0]]
    with pytest.raises(SpecError):
        check_spec(spec, Role.GUARD, POLICY)


def test_check_spec_refuses_a_turn_with_another_user_or_more_writable_places() -> None:
    spec = turn()
    spec["User"] = "0:0"
    with pytest.raises(SpecError, match="unprivileged"):
        check_spec(spec, Role.TURN, POLICY)
    spec = turn()
    spec["HostConfig"]["Tmpfs"]["/var"] = "rw"
    with pytest.raises(SpecError, match="temporary"):
        check_spec(spec, Role.TURN, POLICY)
    with pytest.raises(SpecError, match="only a guard, a turn or a sandbox"):
        check_spec(turn(), Role.VOLUME, POLICY)
    with pytest.raises(SpecError, match="no host configuration"):
        check_spec({"Image": POLICY.instance_image}, Role.TURN, POLICY)


def test_the_guard_carries_the_configured_name_resolvers() -> None:
    assert guard()["HostConfig"]["Dns"] == ["9.9.9.9"]
    check_spec(guard(), Role.GUARD, POLICY)
    other = replace(POLICY, resolvers=("1.1.1.1", "8.8.8.8"))
    spec = guard_spec(other, PROJECT, Side.CONTRACTOR, SUBNET, "10.200.0.19")
    assert spec["HostConfig"]["Dns"] == ["1.1.1.1", "8.8.8.8"]
    check_spec(spec, Role.GUARD, other)


def test_no_name_resolver_is_carried_when_the_configuration_names_none() -> None:
    without = replace(POLICY, resolvers=())
    spec = guard_spec(without, PROJECT, Side.CONTRACTOR, SUBNET, "10.200.0.19")
    assert "Dns" not in spec["HostConfig"]
    check_spec(spec, Role.GUARD, without)


SANDBOX_POLICY = replace(POLICY, sandbox_images=(("tests", DIGEST + "c" * 64),))


def test_a_sandbox_log_is_never_rotated() -> None:
    """ATK-22: the filter believes the kit's lines only in a log that begins with them, so the
    runtime must not rotate it. A runtime that does would drop the head of a flood, and a pair of
    the delivery's own at the point of the cut could be all that was left: the template fixes a
    log driver and a size no sandbox reaches, whatever the daemon's own default is."""
    spec = sandbox_spec(SANDBOX_POLICY, PROJECT, "0a1b2c3d", "tests")
    assert spec["HostConfig"]["LogConfig"] == SANDBOX_LOG
    assert SANDBOX_LOG["Config"]["max-file"] == "1"
    check_spec(spec, Role.SANDBOX, SANDBOX_POLICY)


@pytest.mark.parametrize(
    "log_config",
    [
        None,  # the daemon's default, which may rotate
        {"Type": "json-file", "Config": {}},  # the default size of the daemon is merged in
        {"Type": "json-file", "Config": {"max-size": "10m", "max-file": "3"}},
        {"Type": "json-file", "Config": {"max-size": "1t", "max-file": "3"}},
        {"Type": "local", "Config": {"max-size": "1t", "max-file": "1"}},
        {"Type": "none"},
    ],
)
def test_a_sandbox_whose_log_could_be_rotated_is_refused(log_config: Any) -> None:
    spec = sandbox_spec(SANDBOX_POLICY, PROJECT, "0a1b2c3d", "tests")
    if log_config is None:
        del spec["HostConfig"]["LogConfig"]
    else:
        spec["HostConfig"]["LogConfig"] = log_config
    with pytest.raises(SpecError, match="log of a sandbox is never rotated"):
        check_spec(spec, Role.SANDBOX, SANDBOX_POLICY)


@given(
    project=st.from_regex(r"p_[a-z0-9]{6,32}", fullmatch=True),
    side=st.sampled_from(list(Side)),
    prompt=st.text(min_size=1, max_size=300).filter(lambda s: "\x00" not in s),
    body=st.text(
        alphabet=string.ascii_letters + string.digits + "_-",
        min_size=43,
        max_size=43,
    ),
    third=st.integers(min_value=3, max_value=14),
)
def test_every_specification_the_templates_can_produce_passes_the_allow_list(
    project: str, side: Side, prompt: str, body: str, third: int
) -> None:
    subnet = ipaddress.IPv4Network("10.200.5.64/28")
    hub = str(subnet.network_address + third)
    check_spec(guard_spec(POLICY, project, side, subnet, hub), Role.GUARD, POLICY)
    spec = turn_spec(POLICY, HermesEngine(), project, side, hub, "zph_o_" + body, prompt)
    check_spec(spec, Role.TURN, POLICY)
