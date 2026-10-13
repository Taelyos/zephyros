"""The memory measurement of Z2-15 (US-E10-01, EXG-31): the numbers behind N.

Two projects, each with an Owner and a Contractor instance. First the idle floor: the hub,
the filter and the four guards, with nothing running. Then one scripted conversation with
tool calls in each of the four instances at the same time. Every container of the deployment
is sampled through the runtime's own statistics for the whole run; the report gives the peak
per container and the peak of their sum, which is what N must cover.

On purpose this is not a pass/fail test: it is the measurement the contract asks for. Run it
on the machine class of EXG-31 (a small homelab box, 8 GB of memory), from the repository root:

    ZEPHYROS_RUNTIME_TESTS=1 uv run --locked python -m tests.runtime.measure_memory

It needs the same environment variable as the runtime suite (ZEPHYROS_RUNTIME_TESTS=1) so it
never runs by accident in a check, and a real container runtime.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

from tests.runtime.deployment import Deployment
from tests.runtime.rig import docker
from zephyros.core.ports.agent_engine import Side

SAMPLE_SECONDS = 2.0
IDLE_SECONDS = 20.0
ACTIVE_TIMEOUT = 240.0
SCRIPT = "WRITE /work/note.md\nRUN sleep 20\nRUN sleep 40\nCODE print('done')"
TOOLS_PROMPT = "RUN true"

_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}


def _bytes(text: str) -> int:
    """`docker stats` gives '15.39MiB' or '0B'; return bytes."""
    for unit in ("GiB", "MiB", "KiB", "B"):
        if text.endswith(unit):
            return int(float(text[: -len(unit)]) * _UNITS[unit])
    raise ValueError(f"cannot read a size: {text!r}")


def _sample() -> dict[str, int]:
    """The current memory of every container of a running deployment, by name."""
    done = docker(
        "stats",
        "--no-stream",
        "--format",
        "{{.Name}}\t{{.MemUsage}}",
    )
    found: dict[str, int] = {}
    for line in done.stdout.splitlines():
        name, _, usage = line.partition("\t")
        if name.startswith("zs"):
            found[name] = _bytes(usage.split("/")[0].strip())
    return found


def _sample_while(seconds: float, collected: list[dict[str, int]]) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        collected.append(_sample())
        time.sleep(SAMPLE_SECONDS)


def _turns_running() -> int:
    done = docker("ps", "--format", "{{.Names}}", "--filter", "name=zs-turn-")
    return len([line for line in done.stdout.splitlines() if line.strip()])


def _wait_for_turns(timeout: float, collected: list[dict[str, int]]) -> None:
    """Sample until no turn runs, or the timeout passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _turns_running() > 0:
        collected.append(_sample())
        time.sleep(SAMPLE_SECONDS)


def _report(title: str, samples: list[dict[str, int]]) -> dict[str, int]:
    peaks: dict[str, int] = {}
    for sample in samples:
        for name, used in sample.items():
            peaks[name] = max(peaks.get(name, 0), used)
    totals = [sum(sample.values()) for sample in samples]
    print(f"\n## {title}\n")
    print("| container | peak |")
    print("| --- | --- |")
    for name in sorted(peaks):
        print(f"| `{name}` | {peaks[name] / 1024**2:.1f} MiB |")
    if totals:
        print(f"\nPeak of the sum: **{max(totals) / 1024**2:.0f} MiB**", end="")
        print(f" (steady: {statistics.median(totals) / 1024**2:.0f} MiB)")
    return peaks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default="", help="a state directory (temporary by default)")
    parser.add_argument("--idle-seconds", type=float, default=IDLE_SECONDS)
    arguments = parser.parse_args()
    if os.environ.get("ZEPHYROS_RUNTIME_TESTS") != "1":
        print("set ZEPHYROS_RUNTIME_TESTS=1 to run the measurement", file=sys.stderr)
        return 2
    if arguments.state:
        state = Path(arguments.state)
    else:
        state = Path(tempfile.mkdtemp(prefix="zs-memory-"))
    state.mkdir(parents=True, exist_ok=True)
    memory = Path("/proc/meminfo").read_text()  # the machine, for the record
    machine = next(line for line in memory.splitlines() if line.startswith("MemTotal"))
    print(f"# Memory measurement of two active projects (Z2-15)\nmachine: {machine.strip()}")

    deployment = Deployment(state, pool="10.218.0.0/16")
    deployment.up()
    try:
        projects = [deployment.project() for _ in range(2)]
        # Bring the four guards up: a turn that ends at once leaves the instance waiting.
        for project in projects:
            for side in (Side.OWNER, Side.CONTRACTOR):
                deployment.turn(project, side, TOOLS_PROMPT, seconds=120)
        print("the four instances are started and waiting")
        idle: list[dict[str, int]] = []
        _sample_while(arguments.idle_seconds, idle)
        _report("Idle (hub, filter, four guards)", idle)

        # Four scripted conversations with tool calls, at the same time.
        for project in projects:
            for side in (Side.OWNER, Side.CONTRACTOR):
                deployment.data(
                    "instance.start",
                    project_id=project,
                    side=side.value,
                    prompt=SCRIPT,
                )
        active: list[dict[str, int]] = []
        _wait_for_turns(ACTIVE_TIMEOUT, active)
        if _turns_running() > 0:
            print("warning: a turn was still running at the timeout", file=sys.stderr)
        _report("Active (the four scripted conversations at once)", active)
    finally:
        deployment.down()
    print("\nthe deployment is down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
