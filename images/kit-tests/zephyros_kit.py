"""The pytest plugin of the "tests" kit.

It reads the acceptance tests asked for (`path::name`) from /work/zephyros-request.json, collects
only their files, runs only them, and prints their outcomes on one signed line at the end:

    ZEPHYROS-RUN <nonce>
    ZEPHYROS-OUTCOMES {"outcomes": {"path::name": "passed|failed|error|not_run"},
                       "deselected": ["path::name", ...]} <hmac>

Why a signature is not enough on its own. The nonce and the outcomes both live in this process's
memory. Any code that runs in this interpreter can read the nonce (a plain attribute of this
module, or a read of the process's memory) and rewrite the outcomes before they are signed, so it
would make the kit sign its own forgery. A signature protects a message against another *process*;
it does nothing against code in the *same* process. The audit of 2026-10 showed exactly this: a
delivered module imported by an acceptance test could do `sys.modules["zephyros_kit"].OUTCOMES = …`
and read `_NONCE` — one attribute access, not some exotic memory feat.

So the real defence is to keep every line of delivered code out of this process (ADR 0017):

1. **The judge imports no delivered code.** Before any test file is collected, an import barrier
   (`_WorkImportBarrier`) is installed that refuses to import any module resolved under /work
   (as it is listed, or where a link leads) except the exact acceptance-test files asked for.
   An acceptance test is therefore black-box: it may run the delivery as a *subprocess* and read
   its output, but it may not `import` the delivered code into the judge. A test that tries to
   import it fails to load and is reported as an error, never a pass. The run also ignores the
   project's own configuration and conftests (`-c /dev/null`, `--noconftest`), and keeps the
   working directory off `sys.path` (`python -P`), so a delivered file cannot shadow this plugin.
2. **The judge is not debuggable.** `prctl(PR_SET_DUMPABLE, 0)` makes this process's memory
   unreadable to the unprivileged subprocesses a black-box test spawns, so a child cannot recover
   the nonce by attaching to its parent, whatever `kernel.yama.ptrace_scope` is on the host.
3. **The signed line authenticates against those children.** A black-box test's subprocess shares
   the container's stdout (the log the filter reads), and pytest echoes what a failing test's
   subprocess wrote into that log, after the lines below. The nonce lands only in that log —
   write-only from inside the container — and never in a child's environment or memory, so a child
   can write a line but cannot sign one. The filter believes the results only when it has read
   the whole log, which begins with the run line, and exactly one run line and one signed outcome
   line are present and match: a flood that would push the run line out of the filter's window
   is refused, not believed.

Residual limit, stated plainly (ADR 0017): the acceptance-test *files* asked for do run in this
process, so they must be the Owner's, held by the hub — never files the delivery supplied. A
delivery that could place its own code at a path a criterion names would run in the judge and
could forge. Keeping delivered files out of that set is the hub's job, not the kit's.
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
import secrets
import sys
from collections.abc import Mapping
from importlib.machinery import PathFinder
from pathlib import Path

import pytest

REQUEST = Path("/work/zephyros-request.json")
WORK = "/work"
MARK = "ZEPHYROS-OUTCOMES "
RUN_MARK = "ZEPHYROS-RUN "
PR_SET_DUMPABLE = 4

# The container's own stdout, taken before pytest replaces fd 1 with its capture file: the run line
# and the signed line go there. `os.dup` makes it non-inheritable, so no subprocess gets it, and
# the judge being non-dumpable none can open it through /proc either. What a subprocess can still
# do is write to the same log by other means; it cannot read it back, so the nonce stays unknown.
REAL_OUT = os.fdopen(os.dup(1), "w", buffering=1)
_NONCE = secrets.token_hex(16)


def _asked() -> list[str]:
    try:
        tests = json.loads(REQUEST.read_text(encoding="utf-8"))["tests"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return [t for t in tests if isinstance(t, str) and "::" in t]


ASKED = _asked()
OUTCOMES: dict[str, str] = dict.fromkeys(ASKED, "not_run")
DESELECTED: list[str] = []
BROKEN_FILES: set[str] = set()


def _set_non_dumpable() -> bool:
    """Make this process non-dumpable so a subprocess it spawns cannot read its memory (the
    nonce) by attaching to it, regardless of `kernel.yama.ptrace_scope` on the host. Returns
    whether the flag is set; the kit runs on Linux (the sandbox image), where this holds."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            return False
        return libc.prctl(3) == 0  # PR_GET_DUMPABLE
    except (OSError, AttributeError):  # pragma: no cover  # reason: not reachable on the Linux kit
        return False


class _WorkImportBarrier:
    """A meta-path finder that refuses to import any module that lives under /work other than the
    acceptance-test files asked for. It never imports anything itself: it lets the real finders
    locate the module, and raises `ImportError` only when the module they would load is a file of
    the delivery tree. Stdlib and the kit's own packages (outside /work) pass through untouched.

    A module is judged by where it is listed *and* by where it really is: a link a subprocess made
    in the tree to a file it wrote elsewhere is a delivered module like any other. An asked file
    is let through only where it really was when the run began, so one that a subprocess has since
    replaced by a link is not the file that was asked."""

    def __init__(self, allowed: Mapping[str, str], root: str) -> None:
        self._allowed = dict(allowed)
        self._root = root

    def _under_work(self, path: str) -> bool:
        return path == self._root or path.startswith(self._root + os.sep)

    def find_spec(self, name: str, path: object = None, _target: object = None) -> None:
        try:
            spec = PathFinder.find_spec(name, path)  # type: ignore[arg-type]  # reason: path is the finder's own
        except (ImportError, AttributeError, ValueError):
            return None
        origin = getattr(spec, "origin", None) if spec is not None else None
        if not origin or origin in ("built-in", "frozen", "namespace"):
            return None
        try:
            listed = os.path.abspath(origin)
            real = os.path.realpath(origin)
        except OSError:  # pragma: no cover  # reason: realpath of a found module does not fail
            return None
        inside = self._under_work(listed) or self._under_work(real)
        if inside and self._allowed.get(listed) != real:
            raise ImportError(
                f"the tests kit does not import {name!r} from the delivery tree: an acceptance "
                "test must treat the delivery as a black box (run it, read its output), never "
                "import its code into the judge (ATK-22)"
            )
        return None


def _allowed_paths() -> dict[str, str]:
    """The acceptance-test files asked for, the only /work files the judge is allowed to import,
    as they are listed and where each really is. A file that does not exist is simply not there."""
    paths: dict[str, str] = {}
    for test in ASKED:
        listed = os.path.abspath(os.path.join(WORK, test.split("::", 1)[0]))
        if os.path.isfile(listed):
            paths[listed] = os.path.realpath(listed)
    return paths


def _install_barrier() -> None:
    sys.meta_path.insert(0, _WorkImportBarrier(_allowed_paths(), os.path.realpath(WORK)))


def pytest_load_initial_conftests(
    early_config: pytest.Config, parser: object, args: list[str]
) -> None:
    # The first hook, before pytest collects anything: harden the process while no delivered code
    # can yet run, then print the run line. Non-dumpable first (so no child can ever read the
    # nonce), then the import barrier (so no delivered module enters the judge), then the run line
    # (before any file of the delivery could run). The rewritten arguments are the ones pytest
    # finally parses, so the options the kit relies on live here, not on the command line: the
    # project's own configuration stays ignored (-c /dev/null), no conftest of the delivery is
    # loaded (--noconftest), no cache is written, and a file that does not collect is an error for
    # its tests, not the end of the run.
    if not _set_non_dumpable():
        # Without it a subprocess of a test could read the nonce: fail closed. No line is printed,
        # so every test reads not_run and the run is refused.
        raise pytest.UsageError("the tests kit could not make its process non-dumpable (ATK-22)")
    _install_barrier()
    print(RUN_MARK + _NONCE, file=REAL_OUT, flush=True)
    files = [f for f in sorted({t.split("::", 1)[0] for t in ASKED}) if os.path.isfile(f)]
    args[:] = [
        "-c",
        "/dev/null",
        "--noconftest",
        "-p",
        "no:cacheprovider",
        "--continue-on-collection-errors",
        *(files or ["/dev/null/no-test"]),
    ]


def pytest_collectreport(report: pytest.CollectReport) -> None:
    if report.failed:
        BROKEN_FILES.add(report.nodeid.split("::", 1)[0])


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # Only the tests a criterion named run; the other tests collected from the asked files are
    # deselected and reported as such, so the hub can say which acceptance tests never ran (A2).
    wanted = set(ASKED)
    kept = [item for item in items if item.nodeid in wanted]
    dropped = [item for item in items if item.nodeid not in wanted]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        DESELECTED[:] = sorted({item.nodeid for item in dropped})
    items[:] = kept


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.nodeid not in OUTCOMES:
        return
    if report.when == "call":
        OUTCOMES[report.nodeid] = (
            "passed" if report.passed else ("failed" if report.failed else "not_run")
        )
    elif report.failed:
        OUTCOMES[report.nodeid] = "error"
    elif report.skipped and report.when == "setup":
        OUTCOMES[report.nodeid] = "not_run"


def _outcomes_line() -> str:
    """The signed outcome line. Its payload carries the outcome of every test asked for and the
    tests deselected for want of a criterion; the HMAC under the nonce covers the whole payload,
    so neither the outcomes nor the deselected list can be changed without breaking the pair."""
    payload = json.dumps({"outcomes": OUTCOMES, "deselected": DESELECTED}, sort_keys=True)
    mac = hmac.new(_NONCE.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return MARK + payload + " " + mac


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    for test in ASKED:
        if OUTCOMES[test] == "not_run" and test.split("::", 1)[0] in BROKEN_FILES:
            OUTCOMES[test] = "error"
    # the leading newline keeps the line whole: it may follow pytest's own last write
    print("\n" + _outcomes_line(), file=REAL_OUT, flush=True)
