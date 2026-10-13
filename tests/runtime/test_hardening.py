"""US-E1-23, ATK-32, ATK-04, ATK-06, ATK-35: the hardening of a turn, seen from inside it.

The turn runs a probe image with the template of the filter, on a real runtime, in the namespace
of a real guard. Each attempt is made as the user of the turn (10000), the way an agent would.
The attempts that a tool of Hermes would make are the same system calls; they run against the
Hermes image in Z2-13.
"""

from __future__ import annotations

import pytest

from tests.runtime.rig import Instance, Runtime
from zephyros.core.ports.agent_engine import Side

pytestmark = [pytest.mark.runtime, pytest.mark.acceptance]

WRITABLE = {"/tmp", "/opt/data", "/work", "/dev/mqueue"}


@pytest.fixture(scope="module")
def turn(runtime: Runtime) -> Instance:
    return runtime.bring_up(runtime.project(), Side.CONTRACTOR)


def status_field(runtime: Runtime, container: str, name: str, user: str | None = None) -> str:
    code, out = runtime.sh(container, f"grep '^{name}:' /proc/self/status", user=user)
    assert code == 0, out
    return out.split(":", 1)[1].strip()


# ----------------------------------------------------------------------------- the profile
def test_us_e1_23_the_turn_runs_as_user_10000_with_no_capability(
    runtime: Runtime, turn: Instance
) -> None:
    assert runtime.sh(turn.turn, "id -u; id -g") == (0, "10000\n10000")
    for name in ("CapEff", "CapPrm", "CapBnd", "CapAmb"):
        assert int(status_field(runtime, turn.turn, name), 16) == 0, name


def test_us_e1_23_no_new_privileges_and_the_default_syscall_filter(
    runtime: Runtime, turn: Instance
) -> None:
    assert status_field(runtime, turn.turn, "NoNewPrivs") == "1"
    assert status_field(runtime, turn.turn, "Seccomp") == "2"  # a filter is loaded


def test_us_e1_23_even_root_inside_the_turn_holds_no_capability(
    runtime: Runtime, turn: Instance
) -> None:
    """Not reachable for an agent (no setuid, no-new-privileges); checked as defence in depth."""
    assert int(status_field(runtime, turn.turn, "CapEff", user="0"), 16) == 0


def test_us_e1_23_exactly_the_three_locations_and_the_ipc_mount_are_writable(
    runtime: Runtime, turn: Instance
) -> None:
    code, out = runtime.sh(
        turn.turn,
        "for m in $(awk '{print $2}' /proc/mounts | sort -u); do "
        'f="$m/.zs-write-probe"; '
        'if ( : > "$f" ) 2>/dev/null; then rm -f "$f"; echo "$m"; fi; done',
    )
    assert code == 0, out
    assert set(out.split()) == WRITABLE
    code, out = runtime.sh(
        turn.turn, "( : > /.zs ) 2>&1; ( : > /etc/zs ) 2>&1; ( : > /usr/zs ) 2>&1"
    )
    assert out.count("Read-only file system") == 3, out


def test_us_e1_23_ipc_is_private_and_limits_are_set(runtime: Runtime, turn: Instance) -> None:
    assert runtime.sh(turn.turn, "test -e /dev/shm && echo present || echo absent")[1] == "absent"
    code, pids = runtime.sh(turn.turn, "cat /sys/fs/cgroup/pids.max")
    assert (code, pids) == (0, "128")
    code, memory = runtime.sh(turn.turn, "cat /sys/fs/cgroup/memory.max")
    assert (code, memory) == (0, str(256 * 1024 * 1024))


def test_us_e1_23_the_temporary_directory_does_not_run_programs(
    runtime: Runtime, turn: Instance
) -> None:
    code, out = runtime.sh(turn.turn, "cp /bin/busybox /tmp/bb && /tmp/bb true")
    assert code != 0
    assert "Permission denied" in out


# ----------------------------------------------------------------------------- ATK-32
@pytest.mark.parametrize(
    "attempt",
    [
        "mount -t tmpfs none /mnt",
        "mount -o remount,rw /",
        "unshare -m true",
        "unshare -U -r true",
        "unshare -n true",
        "python3 -c 'import ctypes,sys; l=ctypes.CDLL(None); "
        "sys.exit(0 if l.syscall(250, 0, 0, 0, 0) >= 0 else 1)'",
        "python3 -c 'import ctypes,sys; l=ctypes.CDLL(None); "
        'sys.exit(0 if l.syscall(175, 0, 0, b"") == 0 else 1)\'',
        "test -S /var/run/docker.sock || test -S /run/docker.sock",
        "su -c true root </dev/null",
        "chown root /work",
    ],
    ids=[
        "mount",
        "remount-root",
        "unshare-mount",
        "unshare-user",
        "unshare-net",
        "keyctl",
        "init-module",
        "docker-socket",
        "setuid-su",
        "chown",
    ],
)
def test_atk_32_each_escape_attempt_fails(runtime: Runtime, turn: Instance, attempt: str) -> None:
    code, out = runtime.sh(turn.turn, attempt)
    assert code != 0, f"{attempt} succeeded: {out}"


def test_atk_32_ptrace_reaches_only_the_processes_of_the_turn_itself(
    runtime: Runtime, turn: Instance
) -> None:
    """The default syscall filter of the runtime, which the annex prescribes, lets a process
    trace another of the same user in the same container. It cannot see any other process: the
    turn has its own process namespace."""
    code, out = runtime.sh(turn.turn, "ls /proc | grep -E '^[0-9]+$' | wc -l")
    assert code == 0
    assert int(out) < 10
    code, out = runtime.sh(turn.turn, "cat /proc/[0-9]*/cmdline 2>/dev/null | tr '\\0' ' '")
    assert "observer.py" not in out  # the hub stand-in, in another container
    assert "guard.sh" not in out  # the guard shares the network namespace only
    assert "dockerd" not in out
    assert "containerd" not in out


def test_atk_32_no_setuid_program_gains_anything(runtime: Runtime, turn: Instance) -> None:
    _code, out = runtime.sh(turn.turn, "find / -xdev -perm -4000 -type f 2>/dev/null")
    for program in out.split():
        _code, who = runtime.sh(turn.turn, f"{program} --help >/dev/null 2>&1; id -u")
        assert who.splitlines()[-1] == "10000"


# ----------------------------------------------------------------------------- ATK-04
@pytest.mark.parametrize(
    ("attempt", "target"),
    [
        ("python3 -c \"open('/contract.md','w').write('x')\"", "/contract.md"),
        ("python3 -c \"open('/etc/contract','w').write('x')\"", "/etc/contract"),
        ('perl -e \'open(F, ">/contract.md"); print F "x"; close(F)\'', "/contract.md"),
        ("perl -pi -e 's/root/toor/' /etc/passwd", "/etc/passwd"),
        (
            "cp /usr/bin/python3 /work/renamed && "
            "/work/renamed -c \"open('/srv/x','w').write('x')\"",
            "/srv/x",
        ),
        ('cp /usr/bin/perl /work/p && /work/p -e \'open(F, ">/usr/x"); print F "x"\'', "/usr/x"),
    ],
    ids=[
        "python-root",
        "python-etc",
        "perl-root",
        "perl-inplace",
        "python-renamed",
        "perl-renamed",
    ],
)
def test_atk_04_no_interpreter_writes_outside_the_writable_locations(
    runtime: Runtime, turn: Instance, attempt: str, target: str
) -> None:
    before = runtime.sh(turn.turn, f"sha256sum {target} 2>/dev/null || echo absent")[1]
    runtime.sh(turn.turn, attempt)
    after = runtime.sh(turn.turn, f"sha256sum {target} 2>/dev/null || echo absent")[1]
    assert after == before, f"{attempt} changed {target}"


# ----------------------------------------------------------------------------- ATK-06
def test_atk_06_the_file_system_cannot_be_made_writable(runtime: Runtime, turn: Instance) -> None:
    for attempt in (
        "mount -o remount,rw /",
        "chmod 777 /etc",
        "chattr -i /etc/passwd",
        "unshare -m sh -c 'mount -o remount,rw /'",
    ):
        code, out = runtime.sh(turn.turn, attempt)
        assert code != 0, f"{attempt}: {out}"


def test_atk_06_no_host_path_is_mounted_and_proc_root_is_the_containers_own(
    runtime: Runtime, turn: Instance
) -> None:
    code, mounts = runtime.sh(turn.turn, "cat /proc/mounts")
    assert code == 0
    sources = {line.split()[1]: line.split()[2] for line in mounts.splitlines()}
    for point in sources:
        assert not point.startswith(("/host", "/var/lib/docker", "/Users", "/home")), point
    assert (
        runtime.sh(turn.turn, "ls /proc/1/root/ | sort | tr '\\n' ' '")[1]
        == runtime.sh(turn.turn, "ls / | sort | tr '\\n' ' '")[1]
    )
    code, _out = runtime.sh(turn.turn, "ls /proc/1/root/var/lib/docker 2>&1")
    assert code != 0


def test_atk_06_device_nodes_of_the_host_disks_are_not_there(
    runtime: Runtime, turn: Instance
) -> None:
    code, out = runtime.sh(turn.turn, "ls /dev")
    assert code == 0
    for disk in ("sda", "vda", "nvme0n1", "xvda", "dm-0", "loop0"):
        assert disk not in out.split()
    assert runtime.sh(turn.turn, "mknod /tmp/d b 8 0")[0] != 0


# ----------------------------------------------------------------------------- ATK-35
def test_atk_35_an_instance_of_project_b_sees_no_volume_of_project_a(runtime: Runtime) -> None:
    a = runtime.bring_up(runtime.project(), Side.CONTRACTOR)
    b = runtime.bring_up(runtime.project(), Side.CONTRACTOR)
    runtime.sh(a.turn, "echo secret-of-a > /work/secret; echo secret-of-a > /opt/data/secret")
    code, mounts = runtime.sh(b.turn, "cat /proc/mounts")
    assert a.project_id not in mounts
    for attempt in (
        "ls /var/lib/docker/volumes",
        f"ls /var/lib/docker/volumes/zs-vol-{a.project_id}-contractor-work",
        "cat /proc/1/root/work/secret | grep -q secret-of-a",
        "grep -rqs secret-of-a /work /opt/data /tmp",
        f"ln -s /var/lib/docker/volumes/zs-vol-{a.project_id}-contractor-work/_data /work/l"
        " && cat /work/l/secret",
        f"mount -t volume zs-vol-{a.project_id}-contractor-work /mnt",
    ):
        code, out = runtime.sh(b.turn, attempt)
        assert code != 0, f"{attempt}: {out}"
    assert runtime.sh(a.turn, "cat /work/secret")[1] == "secret-of-a"  # control
