"""ATK-27 to ATK-31 and ATK-86: the sandbox on trial (EXG-05, EXG-38).

The container is built from `sandbox_spec` itself — the filter's template, the rig's policy —
started with a waiting command, and the attempts run inside it by exec. What these tests watch is
the profile and its bite on the real runtime. Two known limits stay outside: the delivered code
shares the container with the kit (EXG-37), and the work volume has no quota of its own (the
writable locations of the profile are capped where the profile says so; the journal records the
question).
"""

from __future__ import annotations

import json
import time

import pytest

from tests.runtime.rig import LAN_ADDRESSES, Runtime, docker

pytestmark = [pytest.mark.runtime, pytest.mark.acceptance]

WRITABLE = {"/dev/mqueue", "/tmp", "/work"}
"""The three writable locations of the annex "Container hardening profile", observed from inside."""


def test_atk_86_the_sandbox_carries_the_hardening_profile(runtime: Runtime) -> None:
    name = runtime.sandbox(runtime.project(), run="00000001")
    spec = json.loads(docker("inspect", name).stdout)[0]
    host = spec["HostConfig"]
    assert spec["Config"]["User"] == "10000:10000"
    assert host["Privileged"] is False
    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"]
    assert not host.get("CapAdd")
    assert host["SecurityOpt"] == ["no-new-privileges"]
    assert host["IpcMode"] == "none"
    assert host["NetworkMode"] == "none"
    assert host["Memory"] == 256 * 1024 * 1024
    assert host["PidsLimit"] == 128
    assert host["NanoCpus"] == 1_000_000_000
    assert set(host["Tmpfs"]) == {"/tmp"}
    mounts = spec["Mounts"]
    assert [(mount["Type"], mount["Destination"]) for mount in mounts] == [("volume", "/work")]
    assert str(mounts[0]["Name"]).startswith("zs-sbx-")
    env = spec["Config"]["Env"]
    assert env[:2] == ["HOME=/tmp", "PYTHONDONTWRITEBYTECODE=1"]
    assert {entry.split("=", 1)[0] for entry in env} <= {
        "HOME",
        "PYTHONDONTWRITEBYTECODE",
        "PATH",
    }
    assert not any("TOKEN" in entry or "ZEPHYROS" in entry for entry in env)
    # what the runtime enforces, read from inside
    assert runtime.sh(name, "cat /sys/fs/cgroup/pids.max") == (0, "128")
    assert runtime.sh(name, "cat /sys/fs/cgroup/memory.max") == (0, str(256 * 1024 * 1024))
    assert runtime.sh(name, "cat /sys/fs/cgroup/cpu.max") == (0, "100000 100000")
    assert runtime.sh(name, "test -e /dev/shm && echo present || echo absent")[1] == "absent"


def test_atk_32_86_each_escape_attempt_fails_in_the_sandbox(runtime: Runtime) -> None:
    name = runtime.sandbox(runtime.project(), run="00000002")
    for attempt in (
        "mount -t tmpfs none /mnt",
        "mount -o remount,rw /",
        "unshare -m true",
        "unshare -U -r true",
        "unshare -n true",
        "su -c true root </dev/null",
        "chown root /work",
    ):
        code, out = runtime.sh(name, attempt)
        assert code != 0, f"{attempt} succeeded: {out}"
    assert runtime.sh(name, "test -S /var/run/docker.sock || test -S /run/docker.sock")[0] != 0


def test_atk_27_no_contract_lives_in_the_sandbox_and_writes_do_not_escape(runtime: Runtime) -> None:
    project = runtime.project()
    name = runtime.sandbox(project, run="00000003")
    runtime.sh(
        name, "printf 'delivered\\n' > /work/calc.py && printf 'check\\n' > /work/test_calc.py"
    )
    # nothing of the contract: the delivered tree is all there is
    _code, out = runtime.sh(
        name,
        f"grep -rl '{project}' /bin /etc /home /opt /root /sbin /srv /tmp /usr /var /work "
        "2>/dev/null; true",
    )
    assert out.strip() == "", out
    _code, out = runtime.sh(name, "find / -xdev -iname '*contract*' 2>/dev/null; true")
    assert out.strip() == "", out
    # exactly the three writable locations; the root file system answers read-only
    _code, out = runtime.sh(
        name,
        "for m in $(awk '{print $2}' /proc/mounts | sort -u); do "
        'f="$m/.zs-write-probe"; '
        'if ( : > "$f" ) 2>/dev/null; then rm -f "$f"; echo "$m"; fi; done',
    )
    assert set(out.split()) == WRITABLE
    _code, out = runtime.sh(name, "( : > /etc/zs ) 2>&1; ( : > /usr/zs ) 2>&1")
    assert out.count("Read-only file system") == 2, out
    # a symlink out of the volume does not open the rest of the file system
    assert runtime.sh(name, "ln -s /etc /work/etc-link && touch /work/etc-link/probe")[0] != 0


def test_atk_28_no_secret_lives_in_the_sandbox(runtime: Runtime) -> None:
    name = runtime.sandbox(runtime.project(), run="00000004")
    _code, out = runtime.sh(name, "env")
    keys = {line.split("=", 1)[0] for line in out.splitlines()}
    assert keys == {"HOME", "HOSTNAME", "PATH", "PWD", "PYTHONDONTWRITEBYTECODE", "SHLVL"}
    # the token of a turn and any Hub-shaped secret: nowhere
    _code, out = runtime.sh(
        name,
        "grep -rl 'zph_' /bin /etc /home /opt /root /sbin /srv /tmp /usr /var /work 2>/dev/null; "
        "true",
    )
    assert out.strip() == "", out
    # the environment of the process the sandbox runs: clean too
    _code, out = runtime.sh(name, "cat /proc/1/environ | tr '\\0' '\\n'")
    assert "zph_" not in out
    # no runtime socket, no metadata service, no credential file
    assert runtime.sh(name, "test -S /var/run/docker.sock")[0] != 0
    assert runtime.reach(name, "tcp", "169.254.169.254", 80) == "blocked"
    _code, out = runtime.sh(name, "ls ~/.git-credentials ~/.netrc /run/secrets 2>&1; true")
    assert "No such file" in out


def test_atk_29_the_sandbox_reaches_nothing(runtime: Runtime) -> None:
    name = runtime.sandbox(runtime.project(), run="00000005")
    hub_ip = runtime.ip_of(runtime.hub, runtime.core)
    targets = [
        {"proto": "tcp", "host": hub_ip, "port": 8080},
        {"proto": "tcp", "host": "10.213.0.1", "port": 8080},
        *({"proto": "tcp", "host": address, "port": 80} for address in LAN_ADDRESSES),
        {"proto": "tcp", "host": "1.1.1.1", "port": 443},
        {"proto": "udp", "host": "8.8.8.8", "port": 53},
    ]
    heard = len(runtime.hits(runtime.hub))
    results = runtime.connect(name, targets)
    assert all(result["result"] == "blocked" for result in results), results
    # and the hub of the rig heard no connection from the sandbox
    assert len(runtime.hits(runtime.hub)) == heard


def test_atk_30_a_verification_sees_nothing_of_the_previous_one(runtime: Runtime) -> None:
    project = runtime.project()
    first = runtime.sandbox(project, run="00000006")
    first_volume = runtime.sandbox_volumes[-1]
    runtime.sh(
        first,
        "echo one > /work/leftover\n"
        "echo one > /tmp/left\n"
        '(python3 -c "import time; time.sleep(600)" &)\n'
        "echo left-behind",
    )
    # the hub destroys the sandbox and its volume at the end of the verification
    docker("rm", "-f", first)
    docker("volume", "rm", "-f", first_volume)
    second = runtime.sandbox(project, run="00000007")
    _code, out = runtime.sh(second, "ls /work; ls /tmp; echo done")
    assert out.strip() == "done", out
    # the container and the volume of the first verification are gone
    assert docker("inspect", first, check=False).returncode != 0
    assert docker("volume", "inspect", first_volume, check=False).returncode != 0
    # a fresh process table: no process of the first verification survives into the second
    _code, out = runtime.sh(second, "ls /proc | grep -E '^[0-9]+$' | wc -l")
    assert 0 < int(out) <= 8, out


def test_atk_31_the_limits_bite_and_the_hub_is_unaffected(runtime: Runtime) -> None:
    name = runtime.sandbox(runtime.project(), run="00000008")
    # a memory bomb crosses the memory limit without taking anything else down
    memory = """python3 - <<'EOF'
blocks = []
try:
    for _ in range(8):
        blocks.append(bytearray(64 * 1024 * 1024))
    print("blocks=%d" % len(blocks))
except MemoryError:
    print("blocked=%d" % len(blocks))
EOF
"""
    code, out = runtime.sh(name, memory, timeout=120)
    assert code in (0, 137), out
    if code == 0:
        assert "blocked=" in out, out
        assert int(out.split("blocked=")[1]) < 8, out
    assert docker("inspect", "-f", "{{.State.Running}}", runtime.hub).stdout.strip() == "true"
    # the temporary file system is capped: a fill stops before 128 MiB
    code, out = runtime.sh(
        name,
        "dd if=/dev/zero of=/tmp/fill bs=1M count=128 2>&1 | tail -1; stat -c %s /tmp/fill",
        timeout=120,
    )
    assert code == 0, out
    size = int(out.strip().splitlines()[-1])
    # the fill stopped at the cap of the temporary file system, whatever dd reported
    assert size == 64 * 1024 * 1024, out
    # a fork bomb reaches the process limit; once its children drain, the sandbox answers again
    forks = """python3 - <<'EOF'
import os
import time

kids = 0
try:
    while True:
        if os.fork() == 0:
            time.sleep(6)
            os._exit(0)
        kids += 1
except OSError:
    pass
print("kids=%d" % kids)
EOF
"""
    code, out = runtime.sh(name, forks, timeout=120)
    assert code == 0, out
    kids = int(out.split("kids=")[1])
    assert 0 < kids < 128, out
    # While the children drain, an exec can be refused by the runtime itself ("unable to
    # spawn stage-2: Resource temporarily unavailable"): that is the process limit biting,
    # not a failure of the run — so the loop reads the count, tolerates a refused exec, and
    # keeps watching until the table empties. If it never empties, the limit itself is broken.
    deadline = time.monotonic() + 30
    drained = False
    out = ""
    while time.monotonic() < deadline:
        _code, out = runtime.sh(name, "cat /sys/fs/cgroup/pids.current")
        last = out.splitlines()[-1] if out else ""
        if last.isdigit() and int(last) <= 6:
            drained = True
            break
        time.sleep(1)
    assert drained, f"the children of the fork bomb never drained: {out!r}"
    assert runtime.sh(name, "echo alive") == (0, "alive")
    assert docker("inspect", "-f", "{{.State.Running}}", runtime.hub).stdout.strip() == "true"
