"""The pytest plugin of the "tests" kit.

It reads the acceptance tests asked for (`path::name`) from /work/zephyros-request.json, collects
only their files, runs only them, and prints their outcomes on one signed line at the end:

    ZEPHYROS-RUN <nonce>
    ZEPHYROS-OUTCOMES {"path::name": "passed" | "failed" | "error" | "not_run"} <hmac>

The nonce is invented here, at the first hook, and only ever written to the container's log —
which is write-only from the inside (a container cannot read back its own stdout). The outcomes
line carries an HMAC of its payload under that nonce, so a line the delivered code writes into
the same log cannot be made to match: the filter checks the pair before believing anything
(ATK-22). The run line is printed before any test file — hence any delivered code — can be
imported, and the run ignores the project's own configuration and conftests (-c /dev/null,
--noconftest): a conftest.py of the delivery is never loaded.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

import pytest

REQUEST = Path("/work/zephyros-request.json")
MARK = "ZEPHYROS-OUTCOMES "
RUN_MARK = "ZEPHYROS-RUN "

# The container's own stdout, taken before pytest replaces fd 1 with its capture file. The two
# lines go there; pytest's saved descriptors are of no use to a forger either, because the log
# it would write to cannot be read back — the nonce stays unknown to the delivered code.
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
BROKEN_FILES: set[str] = set()


def pytest_load_initial_conftests(
    early_config: pytest.Config, parser: object, args: list[str]
) -> None:
    # The run line first: before pytest collects anything, so before any file of the delivery
    # can run. The rewritten arguments are the ones pytest finally parses (Config.parse parses
    # them into the option namespace after this hook), so the options the kit relies on live
    # here, not on the command line: the project's own configuration stays ignored (-c
    # /dev/null), no conftest of the delivery is loaded (--noconftest), no cache is written,
    # and a file that does not collect is an error for its tests, not the end of the run.
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
    wanted = set(ASKED)
    kept = [item for item in items if item.nodeid in wanted]
    dropped = [item for item in items if item.nodeid not in wanted]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
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


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    for test in ASKED:
        if OUTCOMES[test] == "not_run" and test.split("::", 1)[0] in BROKEN_FILES:
            OUTCOMES[test] = "error"
    payload = json.dumps(OUTCOMES, sort_keys=True)
    mac = hmac.new(_NONCE.encode(), payload.encode(), hashlib.sha256).hexdigest()
    # the leading newline keeps the line whole: it may follow pytest's own last write
    print("\n" + MARK + payload + " " + mac, file=REAL_OUT, flush=True)
