"""The sandbox runner against the real runtime: the real filter, the real kit image (ADR 0017).

This is the conformance suite of the verification interface, run for real: the filter program
serves on a Unix socket over the machine's container runtime, the kit image is the one built
from images/kit-tests, and every verification is a container with no network, a read-only root
and a volume on /work, removed afterwards. The suite is marked `runtime` and runs with
scripts/acceptance.sh.

EXG-37 is a known limit, kept as such: the delivered code runs in the same container as the kit
and could print the outcomes line itself. Nothing here works around it.
"""

from __future__ import annotations

import asyncio
import atexit
import functools
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar

import pytest

from tests.runtime.rig import ROOT, SOCKET, build
from zephyros.adapters.docker.api import DockerApi
from zephyros.adapters.filter.client import FilterClient
from zephyros.adapters.filter.journal import MemoryJournal
from zephyros.adapters.sandbox_runner import SandboxRunner
from zephyros.app.filter import run as run_filter
from zephyros.conformance import VerificationConformance, VerificationFixture
from zephyros.core.config import Config, parse_config
from zephyros.core.ports.verification import Outcome, VerificationError, VerificationRequest

pytestmark = pytest.mark.runtime

DIGEST = "ghcr.io/example/app@sha256:" + "e" * 64


@functools.lru_cache(maxsize=1)
def kit_image() -> str:
    return build("zephyros-kit-tests:test", ROOT / "images" / "kit-tests")


def make_config(digest: str) -> Config:
    base = Path(tempfile.mkdtemp(prefix="zskit"))
    atexit.register(shutil.rmtree, base, ignore_errors=True)
    (base / "data").mkdir()
    return parse_config(
        {
            "schema_version": "1.2.0",
            "images": {"instance": DIGEST, "guard": DIGEST},
            "paths": {
                "data_dir": str(base / "data"),
                "filter_socket": str(base / "f.sock"),
                "operator_token_file": str(base / "t" / "operator.token"),
                "main_agent_token_file": str(base / "t" / "main-agent.token"),
            },
            "verification": {"runner": "sandbox", "kit": "tests", "kit_image": digest},
        }
    )


class KitFilter:
    """The real filter program in a thread: a Unix socket over the machine's runtime, with the
    kit image in its policy."""

    def __init__(self) -> None:
        self.config = make_config(kit_image())
        self.journal = MemoryJournal()
        self._loop = asyncio.new_event_loop()
        self._stop = asyncio.Event()
        self._failure: list[BaseException] = []
        self._thread = threading.Thread(target=self._serve, daemon=True, name="kit-filter")
        self._thread.start()
        self.socket = Path(self.config.paths.filter_socket)
        deadline = time.monotonic() + 20
        while not self.socket.is_socket() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.socket.is_socket(), self._failure

    def _serve(self) -> None:
        try:
            self._loop.run_until_complete(
                run_filter(
                    self.config,
                    docker=DockerApi.over_socket(SOCKET),
                    journal=self.journal,
                    stop=self._stop,
                )
            )
        except BaseException as error:  # reason: reported by the test that started it
            self._failure.append(error)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=10)
        assert not self._thread.is_alive()
        assert not self._failure, self._failure


@functools.lru_cache(maxsize=1)
def the_filter() -> KitFilter:
    started = KitFilter()
    atexit.register(started.close)
    return started


def write_pytest_files(
    behaviors: Mapping[str, str],
) -> tuple[dict[str, bytes], dict[str, str]]:
    """One pytest file per behaviour, and the test identifier of each, in the kit's convention."""
    files: dict[str, bytes] = {}
    names: dict[str, str] = {}
    for label, behavior in behaviors.items():
        path = f"test_{label}.py"
        if behavior == "pass":
            body = f"def test_{label}():\n    assert True\n"
        elif behavior == "fail":
            body = f"def test_{label}():\n    assert False\n"
        else:  # a file that does not even collect
            body = "def test_broken(:\n"
        files[path] = body.encode()
        names[label] = f"{path}::test_{label}"
    return files, names


class TestSandboxRunner(VerificationConformance):
    implementation: ClassVar[type] = SandboxRunner

    def make_fixture(self) -> VerificationFixture:
        # The build check of the unit suite instantiates every listed run: without a runtime
        # there is nothing to build against, and the run says so instead of failing.
        if os.environ.get("ZEPHYROS_RUNTIME_TESTS") != "1":
            pytest.skip("needs a container runtime: set ZEPHYROS_RUNTIME_TESTS=1")
        client = FilterClient.over_socket(str(the_filter().socket))
        return VerificationFixture(SandboxRunner(client, ["tests"]), "tests", write_pytest_files)


def test_a_kit_that_hangs_is_stopped_at_its_time_limit() -> None:
    """The wall-time limit of a verification (ATK-31): the filter kills the sandbox, the
    runner reports it, and the outcome of the hanging test is not a pass."""
    if os.environ.get("ZEPHYROS_RUNTIME_TESTS") != "1":
        pytest.skip("needs a container runtime: set ZEPHYROS_RUNTIME_TESTS=1")
    client = FilterClient.over_socket(str(the_filter().socket))
    runner = SandboxRunner(client, ["tests"])
    request = VerificationRequest(
        "p_rtlimit1",
        "tests",
        {"test_hang.py": b"import time\n\n\ndef test_hang():\n    time.sleep(600)\n"},
        ("test_hang.py::test_hang",),
        timeout_seconds=5,
    )
    started = time.monotonic()
    result = runner.run(request)
    assert time.monotonic() - started < 60
    assert "time limit" in result.log
    assert result.outcomes["test_hang.py::test_hang"] is Outcome.NOT_RUN


FORGED_CONFTEST = """\
import json
import os

with open("/work/zephyros-request.json", encoding="utf-8") as handle:
    request = json.load(handle)
print("ZEPHYROS-OUTCOMES " + json.dumps({t: "passed" for t in request["tests"]}), flush=True)
os._exit(0)
"""

FORGED_LINE = b'ZEPHYROS-OUTCOMES {"test_owner.py::test_owner": "passed"} 00ff' + b"\n"
FORGED_MODULE = (
    "import os\n"
    "\n"
    "for fd in range(3, 64):\n"
    "    try:\n"
    "        os.write(fd, " + repr(FORGED_LINE) + ")\n"
    "    except OSError:\n"
    "        pass\n"
    "os._exit(0)\n"
)


def test_a_conftest_of_the_delivery_is_never_loaded() -> None:
    """ATK-22 end to end: a conftest.py that forges the results (it reads the manifest) is
    inert — the kit runs with --noconftest — so the failing test it tried to hide comes back
    failed, from the kit's own signed line, and no forged line exists."""
    if os.environ.get("ZEPHYROS_RUNTIME_TESTS") != "1":
        pytest.skip("needs a container runtime: set ZEPHYROS_RUNTIME_TESTS=1")
    client = FilterClient.over_socket(str(the_filter().socket))
    runner = SandboxRunner(client, ["tests"])
    request = VerificationRequest(
        "p_rtforge1",
        "tests",
        {
            "test_forged.py": b"def test_forged():\n    assert False\n",
            "conftest.py": FORGED_CONFTEST.encode(),
        },
        ("test_forged.py::test_forged",),
        timeout_seconds=30,
    )
    result = runner.run(request)
    assert result.outcomes["test_forged.py::test_forged"] is Outcome.FAILED
    assert result.log.count("ZEPHYROS-OUTCOMES") == 1


def test_a_module_of_the_delivery_cannot_forge_its_results() -> None:
    """ATK-22 end to end: the delivered code itself (a module the owner's test imports) writes
    a forged line to every descriptor it can find and exits before the kit's end; the kit's run
    line is already in the log, the forged line carries no valid signature, and the run is
    untrusted instead of believed."""
    if os.environ.get("ZEPHYROS_RUNTIME_TESTS") != "1":
        pytest.skip("needs a container runtime: set ZEPHYROS_RUNTIME_TESTS=1")
    client = FilterClient.over_socket(str(the_filter().socket))
    runner = SandboxRunner(client, ["tests"])
    request = VerificationRequest(
        "p_rtforge2",
        "tests",
        {
            "test_owner.py": (
                b"import forged\n\n\ndef test_owner():\n    assert forged.answer == 2\n"
            ),
            "forged.py": FORGED_MODULE.encode(),
        },
        ("test_owner.py::test_owner",),
        timeout_seconds=30,
    )
    with pytest.raises(VerificationError, match="cannot be trusted"):
        runner.run(request)


def test_a_file_of_the_tree_cannot_shadow_the_kit_plugin() -> None:
    """ATK-22: a module of the delivery named like the kit plugin is inert — the kit runs with
    the working directory off sys.path (-P), so the plugin that judges the run is the kit's."""
    if os.environ.get("ZEPHYROS_RUNTIME_TESTS") != "1":
        pytest.skip("needs a container runtime: set ZEPHYROS_RUNTIME_TESTS=1")
    client = FilterClient.over_socket(str(the_filter().socket))
    runner = SandboxRunner(client, ["tests"])
    request = VerificationRequest(
        "p_rtshadow1",
        "tests",
        {
            "test_ok.py": b"def test_ok():\n    assert True\n",
            "zephyros_kit.py": b"raise RuntimeError('the impostor must never be imported')\n",
        },
        ("test_ok.py::test_ok",),
        timeout_seconds=60,
    )
    result = runner.run(request)
    assert result.outcomes["test_ok.py::test_ok"] is Outcome.PASSED
