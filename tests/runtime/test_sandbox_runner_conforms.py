"""The sandbox runner against the real runtime: the real filter, the real kit image (ADR 0017).

This is the conformance suite of the verification interface, run for real: the filter program
serves on a Unix socket over the machine's container runtime, the kit image is the one built
from images/kit-tests, and every verification is a container with no network, a read-only root
and a volume on /work, removed afterwards. The suite is marked `runtime` and runs with
scripts/acceptance.sh.

The forgery-integrity tests (ATK-22, EXG-37) are the point of this suite now: they reproduce the
audit's attack (a delivered module rewriting the kit's outcomes and reading its nonce) and try
several more ways to subvert the run (a monkeypatched framework, a delivered conftest, an atexit
handler, a subprocess reading the judge, a flooded log, a subprocess writing to the log and
killing the judge). Each shows that delivered code never reaches a believed pass, because the kit
keeps it out of the judging process and the filter reads the log whole (ADR 0017).
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

from tests.runtime.rig import ROOT, SOCKET, build, docker
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


def runner() -> SandboxRunner:
    """A sandbox runner on the real filter; skips when there is no container runtime."""
    if os.environ.get("ZEPHYROS_RUNTIME_TESTS") != "1":
        pytest.skip("needs a container runtime: set ZEPHYROS_RUNTIME_TESTS=1")
    return SandboxRunner(FilterClient.over_socket(str(the_filter().socket)), ["tests"])


def test_a_kit_that_hangs_is_stopped_at_its_time_limit() -> None:
    """The wall-time limit of a verification (ATK-31): the filter kills the sandbox, the
    runner reports it, and the outcome of the hanging test is not a pass."""
    request = VerificationRequest(
        "p_rtlimit1",
        "tests",
        {"test_hang.py": b"import time\n\n\ndef test_hang():\n    time.sleep(600)\n"},
        ("test_hang.py::test_hang",),
        timeout_seconds=10,
    )
    started = time.monotonic()
    result = runner().run(request)
    assert time.monotonic() - started < 60
    assert "time limit" in result.log
    assert result.outcomes["test_hang.py::test_hang"] is Outcome.NOT_RUN
    # AN-023: the run says it reached its limit, and which test was under way
    assert result.stopped_at == 10
    found = result.timeouts["test_hang.py::test_hang"]
    assert found.phase == "run"
    assert 0 < found.seconds <= result.duration_seconds


def test_a_test_that_reaches_a_time_limit_of_its_own_is_told_from_an_error() -> None:
    """AN-023: an acceptance test whose fixture runs the delivery with a limit, and the
    delivery is slower: the kit says which test timed out, in which phase, after how long and at
    which limit; a test that breaks for another reason is an error with no time limit."""
    test_file = (
        "import subprocess, sys\n\nimport pytest\n\n\n"
        "@pytest.fixture(scope='module')\n"
        "def run():\n"
        "    return subprocess.run([sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "                          timeout=1)\n\n\n"
        "def test_slow(run):\n    assert run.returncode == 0\n\n\n"
        "def test_slow_too(run):\n    assert run.returncode == 0\n\n\n"
        "@pytest.fixture\n"
        "def broken():\n    raise RuntimeError('not a time limit')\n\n\n"
        "def test_broken(broken):\n    assert True\n"
    )
    tests = (
        "test_bench.py::test_slow",
        "test_bench.py::test_slow_too",
        "test_bench.py::test_broken",
    )
    request = VerificationRequest(
        "p_rttimeout", "tests", {"test_bench.py": test_file.encode()}, tests, timeout_seconds=60
    )
    result = runner().run(request)
    assert {result.outcomes[test] for test in tests} == {Outcome.ERROR}
    assert set(result.timeouts) == {"test_bench.py::test_slow", "test_bench.py::test_slow_too"}
    for test in ("test_bench.py::test_slow", "test_bench.py::test_slow_too"):
        found = result.timeouts[test]
        assert (found.phase, found.limit) == ("setup", 1.0)
        assert found.seconds >= 1.0  # the first time, for the test that shares the fixture too
    assert result.stopped_at is None


# --------------------------------------------------------------- forgery integrity (ATK-22)
# The point of this suite: whatever the delivered code does, the recorded outcome is the real
# one, or the run is not believed. The defence is that no delivered code runs in the process
# that records the outcomes (ADR 0017) — the kit refuses to import any /work module but the
# asked test files, so an acceptance test is black box — and that the filter believes the log
# only when it holds the kit's lines whole. Each test below plays one way the audit's forgery,
# or a variant of it, could try to reach a believed pass, and shows it cannot.
#
# A run the filter does not believe makes every outcome `error`, canaries included, and the
# runner refuses it (`VerificationError`: a canary of the hub did not fail); a run in which only
# the Owner's file failed to load comes back with that one test as `error`.

# The audit's attack, and two more ways for a delivered module to reach a believed pass, each of
# which needs the module to be imported into the judge. The forgeries spare the canaries (which
# the delivery recognises by name) so that only the barrier stands in the way.
FORGERS = {
    # rewrite the kit's outcomes (a dict that swallows what pytest records) and read its nonce
    "rewrites_the_kit_state": r"""
import sys

kit = sys.modules["zephyros_kit"]
nonce = kit._NONCE  # the audit's "read the nonce": a plain attribute of the module


class _Stuck(dict):
    def __setitem__(self, key, value):
        pass


kit.OUTCOMES = _Stuck({test: ("failed" if "canary" in test else "passed") for test in kit.ASKED})
""",
    # forge from an exit handler, so it runs after the real tests
    "forges_from_an_atexit_handler": r"""
import atexit
import sys

kit = sys.modules["zephyros_kit"]
atexit.register(lambda: kit.OUTCOMES.update(dict.fromkeys(kit.ASKED, "passed")))
""",
    # monkeypatch the framework so that every report says passed
    "patches_the_framework": r"""
import _pytest.reports

_pytest.reports.TestReport.passed = property(
    lambda self: self.when != "call" or "canary" not in self.nodeid
)
_pytest.reports.TestReport.failed = property(
    lambda self: self.when == "call" and "canary" in self.nodeid
)
""",
}


@pytest.mark.parametrize("forger", FORGERS)
def test_a_delivered_module_cannot_be_imported_into_the_judge(forger: str) -> None:
    """ATK-22, the audit's attack end to end (criterion 1). An acceptance test imports a
    delivered module that tampers with the judge to make a failing test look passed. The kit
    refuses to import the module into the judging process, so the import fails, the test is an
    error rather than a believed pass, and the barrier's reason is in the log. On `main` (no
    barrier) the import succeeds and the recorded outcome is the forged one."""
    request = VerificationRequest(
        f"p_rtimport{list(FORGERS).index(forger)}",
        "tests",
        {
            "test_owner.py": b"import forger\n\n\ndef test_owner():\n    assert False\n",
            "forger.py": FORGERS[forger].encode(),
        },
        ("test_owner.py::test_owner",),
        timeout_seconds=60,
    )
    result = runner().run(request)
    assert result.outcomes["test_owner.py::test_owner"] is Outcome.ERROR
    assert "black box" in result.log


def test_a_link_made_during_the_run_does_not_get_a_delivered_module_in() -> None:
    """ATK-22: the barrier judges a module by where it is listed as well as by where it really
    is. A subprocess of a first test writes a forger outside the tree and links `calc.py` to it;
    a second test, which still imports `calc` as the tests of this kit did before it was black
    box, must not get the forger into the judge — the import is refused, and the test fails
    for it. Judged by where it really is alone, the link would have been let through."""
    maker = (
        "import os\n\n"
        f"open('/tmp/forged_calc.py', 'w').write({FORGERS['rewrites_the_kit_state']!r})\n"
        "os.symlink('/tmp/forged_calc.py', '/work/calc.py')\n"
    )
    first = (
        "import subprocess, sys\n\n\n"
        "def test_first():\n"
        "    subprocess.run([sys.executable, 'maker.py'], check=True)\n"
    )
    second = "def test_second():\n    import calc  # noqa: F401\n\n    assert False\n"
    request = VerificationRequest(
        "p_rtlink",
        "tests",
        {
            "test_a_first.py": first.encode(),
            "test_b_second.py": second.encode(),
            "maker.py": maker.encode(),
        },
        ("test_a_first.py::test_first", "test_b_second.py::test_second"),
        timeout_seconds=60,
    )
    result = runner().run(request)
    assert result.outcomes["test_a_first.py::test_first"] is Outcome.PASSED
    assert result.outcomes["test_b_second.py::test_second"] is Outcome.FAILED
    assert "black box" in result.log


def forged_cache(source: bytes, forged: str) -> tuple[str, bytes]:
    """The name and the content of a bytecode cache of `forged` that the pytest of the kit image
    takes for the cache of `source` at /work/test_owner.py: made by the Python and the pytest of
    the image itself, so that it cannot be refused for a version it was not made with."""
    script = (
        "import importlib.util, marshal, sys, pytest\n"
        f"source, forged = {source!r}, {forged!r}\n"
        "code = compile(forged, '/work/test_owner.py', 'exec')\n"
        "header = importlib.util.MAGIC_NUMBER + bytes(8) + len(source).to_bytes(4, 'little')\n"
        "print(f'{sys.implementation.cache_tag}-pytest-{pytest.__version__}')\n"
        "print((header + marshal.dumps(code)).hex())\n"
    )
    done = docker("run", "--rm", "--entrypoint", "python", kit_image(), "-c", script)
    tag, content = done.stdout.split()
    return f"__pycache__/test_owner.{tag}.pyc", bytes.fromhex(content)


def test_a_bytecode_cache_shipped_with_the_delivery_does_not_replace_the_source() -> None:
    """ATK-22: the judge compiles the asked test files, and would run a cache the delivery put
    next to one in place of its source when the time and size recorded in it match: the tree is
    extracted with a fixed time, and the size of a file is a guess away. The kit never reads a
    cache from the tree (it points the import machinery at a directory that does not exist),
    so the source is what runs. The cache is made inside the image, with its Python and pytest."""
    source = b"def test_owner():\n    assert False\n"
    forged = FORGERS["rewrites_the_kit_state"] + "\n\ndef test_owner():\n    assert False\n"
    runner()  # builds the image
    name, content = forged_cache(source, forged)
    request = VerificationRequest(
        "p_rtcache",
        "tests",
        {"test_owner.py": source, name: content},
        ("test_owner.py::test_owner",),
        timeout_seconds=60,
    )
    result = runner().run(request)
    assert result.outcomes["test_owner.py::test_owner"] is Outcome.FAILED


def test_an_asked_file_rewritten_during_the_run_is_the_one_that_was_asked_for_that_runs() -> None:
    """ATK-22: an Owner's test file that runs the delivery as it is imported (to build its
    parameters, say) gives the delivery time to rewrite another asked file that has not been
    imported yet; imported as it then is, the delivery's code would run in the judge. The kit
    keeps the asked files as they were when the run began, and imports those: the second test
    runs as the Owner wrote it (and fails), whatever the tree says by then."""
    forged = FORGERS["rewrites_the_kit_state"] + "\n\ndef test_second():\n    pass\n"
    rewriter = (
        "import pathlib\n\n"
        f"pathlib.Path('/work/test_b_second.py').write_text({forged!r}, encoding='utf-8')\n"
    )
    first = (
        "import subprocess, sys\n\n"
        "subprocess.run([sys.executable, 'rewriter.py'], check=True)\n\n\n"
        "def test_first():\n"
        "    pass\n"
    )
    request = VerificationRequest(
        "p_rtrewrite",
        "tests",
        {
            "test_a_first.py": first.encode(),
            "test_b_second.py": b"def test_second():\n    assert False\n",
            "rewriter.py": rewriter.encode(),
        },
        ("test_a_first.py::test_first", "test_b_second.py::test_second"),
        timeout_seconds=60,
    )
    result = runner().run(request)
    assert result.outcomes["test_a_first.py::test_first"] is Outcome.PASSED
    assert result.outcomes["test_b_second.py::test_second"] is Outcome.FAILED


def test_a_conftest_and_a_pytest_ini_of_the_delivery_are_never_loaded() -> None:
    """ATK-22: a conftest.py, or a plugin named by a pytest.ini, that forges the results the
    moment it loads is inert — the kit ignores the project's configuration and runs with
    --noconftest — so the failing test it tried to hide comes back failed, from the kit's own
    signed line, and no other outcome line exists."""
    loud = b"import os\nprint('ZEPHYROS-OUTCOMES forged', flush=True)\nos._exit(0)\n"
    request = VerificationRequest(
        "p_rtconfig",
        "tests",
        {
            "test_forged.py": b"def test_forged():\n    assert False\n",
            "conftest.py": loud,
            "forged_plugin.py": loud,
            "pytest.ini": b"[pytest]\naddopts = -p forged_plugin\n",
        },
        ("test_forged.py::test_forged",),
        timeout_seconds=60,
    )
    result = runner().run(request)
    assert result.outcomes["test_forged.py::test_forged"] is Outcome.FAILED
    assert result.log.count("ZEPHYROS-OUTCOMES") == 1


def test_a_file_of_the_tree_cannot_shadow_the_kit_plugin() -> None:
    """ATK-22: a module of the delivery named like the kit plugin is inert — the kit runs with
    the working directory off sys.path (-P), so the plugin that judges the run is the kit's."""
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
    result = runner().run(request)
    assert result.outcomes["test_ok.py::test_ok"] is Outcome.PASSED


# What a black-box test may do — run the delivery as a subprocess — is the one door left, so the
# next three tests are the delivery trying everything that door gives it: reading the judge,
# flooding the log, writing to it directly and stopping the judge.
PEEK = r"""
import os

# the manifest is the one file the kit reads: rewrite it, to see whether the run notices
manifest = "/work/zephyros-request.json"
os.chmod(manifest, 0o644)
with open(manifest, "w", encoding="utf-8") as handle:
    handle.write('{"tests": []}')

ppid = os.getppid()
for what in ("mem", "environ"):
    try:
        open("/proc/%d/%s" % (ppid, what), "rb").close()
        print(what, "ALLOWED")
    except OSError:
        print(what, "denied")
try:
    os.listdir("/proc/%d/fd" % ppid)
    print("fd ALLOWED")
except OSError:
    print("fd denied")
try:
    os.close(os.open("/proc/%d/fd/1" % ppid, os.O_WRONLY))
    print("stdout ALLOWED")
except OSError:
    print("stdout denied")
"""


def test_a_black_box_subprocess_cannot_reach_into_the_judge() -> None:
    """ATK-22: the judge is non-dumpable (`PR_SET_DUMPABLE=0`), so the subprocess of a black-box
    test is refused its memory, its environment and its descriptors — whatever the host's ptrace
    policy — and cannot recover the nonce, or write through the judge's own stdout. Without the
    flag these are open to a process of the same user (the descriptors always, the memory and the
    environment when `kernel.yama.ptrace_scope` is 0), and the test fails. The subprocess also
    rewrites the manifest the kit reads, which changes nothing: the kit read it before any
    delivered code ran."""
    test = (
        "import subprocess, sys\n\n\n"
        "def test_peek():\n"
        "    out = subprocess.run([sys.executable, 'peek.py'], capture_output=True, text=True)\n"
        "    assert out.stdout.splitlines() == [\n"
        "        'mem denied', 'environ denied', 'fd denied', 'stdout denied'\n"
        "    ], out.stdout + out.stderr\n"
    )
    request = VerificationRequest(
        "p_rtpeek",
        "tests",
        {"test_peek.py": test.encode(), "peek.py": PEEK.encode()},
        ("test_peek.py::test_peek",),
        timeout_seconds=60,
    )
    result = runner().run(request)
    assert result.outcomes["test_peek.py::test_peek"] is Outcome.PASSED


# A pair of the delivery's own: a nonce it chose, and the signature that matches it. It spares
# the canaries, which it recognises by name, and marks everything else passed.
FORGED_PAIR = r"""
import hashlib
import hmac
import json

tests = json.load(open("/work/zephyros-request.json", encoding="utf-8"))["tests"]
claimed = {t: ("failed" if "canary" in t else "passed") for t in tests}
nonce = "chosen-by-the-delivery"
# the shape of the payload today, and the flat one of the versions before it: the delivery does
# not know which filter reads it
payload = json.dumps({"outcomes": claimed, "deselected": [], **claimed}, sort_keys=True)
mac = hmac.new(nonce.encode(), payload.encode(), hashlib.sha256).hexdigest()
pair = "ZEPHYROS-RUN " + nonce + "\nZEPHYROS-OUTCOMES " + payload + " " + mac + "\n"
"""


def test_a_delivery_that_floods_the_log_with_a_forged_pair_is_not_believed() -> None:
    """ATK-22: pytest echoes the output of a failing test after the kit's signed line, so the
    delivery's subprocess can print a flood and a pair of its own there, and the flood pushes the
    kit's real lines out of any window the filter reads. A filter that read only the tail of the
    log believed the forged pair and reported the failed test as passed; the filter now reads the
    whole log, finds both pairs, and believes neither — the runner refuses the run."""
    flooder = FORGED_PAIR + 'print("x" * 250_000)\nprint(pair, end="")\n'
    test = (
        "import subprocess, sys\n\n\n"
        "def test_flood():\n"
        "    subprocess.run([sys.executable, 'flooder.py'], check=True)\n"
        "    assert False, 'the delivery does not do what it should'\n"
    )
    request = VerificationRequest(
        "p_rtflood",
        "tests",
        {"test_flood.py": test.encode(), "flooder.py": flooder.encode()},
        ("test_flood.py::test_flood",),
        timeout_seconds=60,
    )
    with pytest.raises(VerificationError, match="cannot be trusted"):
        runner().run(request)


@pytest.mark.parametrize("stop", ["SIGKILL", "SIGTERM", "SIGINT"])
def test_a_delivery_that_writes_to_the_log_and_stops_the_judge_is_not_believed(stop: str) -> None:
    """ATK-22, the strongest attempt the one door allows: the subprocess reaches the container's
    own stdout through the init process (`/proc/1/fd/1`, which it may open), writes a flood and
    a pair of its own, then stops the judge with a signal so that the kit's own line is missing
    or comes after. The kit's run line is still the first of the log, so the forged pair is a
    second one and nothing is believed; and had the write been refused, a judge that was stopped
    leaves no outcome for the test it was running, so the canaries do not come back failed."""
    killer = (
        FORGED_PAIR
        + "import os\nimport signal\n\n"
        + "try:\n"
        + '    log = os.open("/proc/1/fd/1", os.O_WRONLY)\n'
        + '    os.write(log, b"x" * 250_000 + b"\\n")\n'
        + "    os.write(log, pair.encode())\n"
        + "except OSError:\n"
        + "    pass\n"
        + f"os.kill(os.getppid(), signal.{stop})\n"
    )
    test = (
        "import subprocess, sys\n\n\n"
        "def test_kill():\n"
        "    subprocess.run([sys.executable, 'killer.py'], check=True)\n"
    )
    request = VerificationRequest(
        f"p_rtstop{stop.lower()}",
        "tests",
        {"test_kill.py": test.encode(), "killer.py": killer.encode()},
        ("test_kill.py::test_kill",),
        timeout_seconds=60,
    )
    with pytest.raises(VerificationError, match="cannot be trusted"):
        runner().run(request)


def test_a_black_box_test_passes_and_the_kit_reports_the_tests_it_did_not_run() -> None:
    """The positive case and the deselected list (interface 0.2.0, audit A2). An acceptance test
    exercises the delivery as a subprocess and passes; a second test in the same file is not
    named by a criterion, so the kit does not run it and reports it as deselected."""
    test_file = (
        "import subprocess, sys\n\n\n"
        "def test_add():\n"
        "    out = subprocess.run(\n"
        "        [sys.executable, '-c', 'from calc import add; print(add(1, 2))'],\n"
        "        capture_output=True, text=True,\n"
        "    )\n"
        "    assert out.stdout.strip() == '3', out.stderr\n\n\n"
        "def test_not_named():\n"
        "    assert False\n"
    )
    request = VerificationRequest(
        "p_rtblackbox",
        "tests",
        {
            "calc.py": b"def add(a, b):\n    return a + b\n",
            "test_calc.py": test_file.encode(),
        },
        ("test_calc.py::test_add",),
        timeout_seconds=60,
    )
    result = runner().run(request)
    assert result.outcomes["test_calc.py::test_add"] is Outcome.PASSED
    assert result.deselected == ("test_calc.py::test_not_named",)
