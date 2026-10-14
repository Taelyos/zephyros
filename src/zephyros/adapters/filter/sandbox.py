"""Sandbox runs in the filter: one verification, one container, one volume, destroyed after.

The hub sends a tar: the files to verify, and `zephyros-request.json` (the project, the kit, the
acceptance tests, the timeout). The filter checks the tar member by member (regular files only,
plain relative paths, size limits), rebuilds it owned by the user of the sandbox, and runs the
kit image from its template with no network. The kit prints a run line (`ZEPHYROS-RUN <nonce>`)
and one signed outcome line (`ZEPHYROS-OUTCOMES <json> <hmac>`); the outcomes are believed only
when exactly one of each is present and the signature matches — the nonce lands in the container's
log, which cannot be read back from the inside, so a line the delivered code writes cannot be
made to match. A test without an outcome is reported as not run.

Integrity of the outcomes (EXG-37, ATK-22, ADR 0017): the kit keeps every line of delivered code
out of the process that records the outcomes — it refuses to import any delivered module and runs
acceptance tests black-box — and makes that process non-dumpable. So the signature here is not
guarding against code in the kit's own process (there is none); it tells the kit's single line
apart from any line a black-box test's *subprocess* writes to the shared container log. Such a
subprocess has neither the nonce (it never leaves the judge's memory) nor a way to read it.

The log is the one channel the delivered code does reach: pytest echoes the output of a failing
test after the kit's signed line, so a delivery can write as much as it likes there. The run line
is what anchors the signature, and it is the first thing the kit prints; so the filter reads the
log from its beginning, as a stream (up to `LOG_READ_LIMIT`), and believes nothing in a log that
was cut or that does not begin with it — otherwise a flood could push the kit's lines out of
the window and leave a pair of the delivery's own as the only one.

The kit's manifest carries the SHA-256 of each file a test is asked from, measured here on the
archive, so the kit can hold the files it runs to what was sent.

Residual limit: the acceptance-test files themselves run in the judge, so the hub must hold them;
a delivery that supplied a file at a named test path would run in the judge and could forge.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import re
import secrets
import tarfile
import time
from collections.abc import Callable
from typing import Any

from zephyros.adapters.docker.api import DockerApi, DockerError
from zephyros.adapters.filter.journal import Journal
from zephyros.core.broker import BrokerError, SandboxManifest, parse_sandbox_manifest
from zephyros.core.containers import (
    INSTANCE_USER,
    WORK_PATH,
    Policy,
    Role,
    SpecError,
    check_spec,
    sandbox_name,
    sandbox_spec,
    sandbox_volume_spec,
)

MANIFEST = "zephyros-request.json"
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_MEMBERS = 20_000
OUTCOME_MARK = "ZEPHYROS-OUTCOMES "
RUN_MARK = "ZEPHYROS-RUN "
OUTCOMES = ("passed", "failed", "error", "not_run")
LOG_LIMIT = 20_000
LOG_READ_LIMIT = 16 * 1024 * 1024
"""The most of a sandbox's log the filter reads to find the kit's lines, from its beginning, as a
stream: a log longer than this is not believed (ATK-22), and a flood does not reach the memory of
the filter. `LOG_LIMIT` is only what is kept as evidence."""
_PATH = re.compile(r"^(?!\.{1,2}$)(?!.*\.\.)(?!/)[A-Za-z0-9_.][A-Za-z0-9_./ +@=-]{0,250}$")
_UID, _GID = (int(part) for part in INSTANCE_USER.split(":"))


def read_archive(raw: bytes) -> tuple[SandboxManifest, bytes]:
    """The manifest, and the files rebuilt as a clean tar. BrokerError for anything else."""
    if len(raw) > MAX_ARCHIVE:
        raise BrokerError("too_large", "the archive is too large")
    try:
        source = tarfile.open(fileobj=io.BytesIO(raw), mode="r:")  # noqa: SIM115  # reason: closed below
    except tarfile.TarError:
        raise BrokerError("bad_request", "the body is not a tar archive") from None
    manifest: SandboxManifest | None = None
    clean = io.BytesIO()
    with source, tarfile.open(fileobj=clean, mode="w:") as target:
        members = source.getmembers()
        if len(members) > MAX_MEMBERS:
            raise BrokerError("too_large", "the archive has too many members")
        seen: set[str] = set()
        for member in members:
            if member.isdir():
                continue
            if not member.isreg():
                raise BrokerError("bad_request", "only regular files are accepted")
            name = member.name
            if not _PATH.fullmatch(name) or name in seen or name.endswith("/"):
                raise BrokerError("bad_request", "a member has a path that is not plain")
            handle = source.extractfile(member)
            data = handle.read() if handle is not None else b""
            if name == MANIFEST:
                if manifest is not None:
                    raise BrokerError("bad_request", f"the archive has two {MANIFEST}")
                manifest = parse_sandbox_manifest(data)
                continue
            seen.add(name)
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            info.uid, info.gid = _UID, _GID
            info.mtime = 0
            target.addfile(info, io.BytesIO(data))
        if manifest is None:
            raise BrokerError("bad_request", f"the archive has no {MANIFEST}")
        info = tarfile.TarInfo(MANIFEST)
        body = json.dumps({"tests": list(manifest.tests)}).encode()
        info.size, info.mode, info.uid, info.gid = len(body), 0o444, _UID, _GID
        target.addfile(info, io.BytesIO(body))
    return manifest, clean.getvalue()


def parse_outcomes(log: str, tests: tuple[str, ...]) -> tuple[dict[str, str], tuple[str, ...]]:
    """The outcome of each test asked for and the tests the kit deselected, from its signed line.

    The kit prints a run line carrying a nonce (which only ever lands in the container's log —
    write-only from the inside) and one outcome line signed with it. The signed payload is
    `{"outcomes": {name: outcome}, "deselected": [name, ...]}`, so the HMAC covers both. An
    honest kit prints the pair once, the run line first of the whole log; anything else — a
    missing run line, a log that begins with something else (it lost its head), a second line, a
    signature that does not match — means the log was written by someone else too (a black-box
    test's subprocess can reach it, ATK-22), and nothing is believed: every test reads `error`
    and nothing is reported deselected, so the run verifies nothing. A run that printed no
    outcome line at all (it died before its end) claims nothing: its tests read `not_run`."""
    runs: list[str] = []
    lines: list[str] = []
    first = ""
    for line in log.splitlines():
        if not first and line.strip():
            first = line
        if line.startswith(RUN_MARK):
            runs.append(line[len(RUN_MARK) :].strip())
        elif line.startswith(OUTCOME_MARK):
            lines.append(line[len(OUTCOME_MARK) :])
    if not lines and len(runs) <= 1:
        return dict.fromkeys(tests, "not_run"), ()
    # The run line is the first thing an honest kit prints: a log that begins with anything else
    # has lost its head, and the pair left at its end is no longer anchored to the kit's nonce.
    if len(runs) != 1 or len(lines) != 1 or not first.startswith(RUN_MARK):
        return dict.fromkeys(tests, "error"), ()
    payload, _, mac = lines[0].rpartition(" ")
    if not payload or not mac:
        return dict.fromkeys(tests, "error"), ()
    expected = hmac.new(runs[0].encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, mac):
        return dict.fromkeys(tests, "error"), ()
    try:
        found = json.loads(payload)
    except ValueError:
        return dict.fromkeys(tests, "error"), ()
    if not isinstance(found, dict) or not isinstance(found.get("outcomes"), dict):
        return dict.fromkeys(tests, "error"), ()
    reported = found["outcomes"]
    outcomes = {
        test: str(reported[test]) if reported.get(test) in OUTCOMES else "not_run" for test in tests
    }
    raw = found.get("deselected", [])
    items = raw if isinstance(raw, list) else []
    deselected = tuple(item for item in items if isinstance(item, str))
    return outcomes, deselected


def run_sandbox(
    api: DockerApi,
    policy: Policy,
    journal: Journal,
    manifest: SandboxManifest,
    archive: bytes,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    new_run: Callable[[], str] = lambda: secrets.token_hex(4),
) -> dict[str, Any]:
    run = new_run()
    name = sandbox_name(manifest.project_id, run)
    try:
        spec = sandbox_spec(policy, manifest.project_id, run, manifest.kit)
        check_spec(spec, Role.SANDBOX, policy)
    except SpecError as error:
        journal.record("refused", "sandbox.run", "policy", project_id=manifest.project_id)
        raise BrokerError("policy", str(error)) from None
    started = monotonic()
    exit_code: int | None = None
    log = ""
    try:
        api.create_volume(sandbox_volume_spec(manifest.project_id, run))
        api.create_container(name, spec)
        api.put_archive(name, WORK_PATH, archive)
        api.start(name)
        exit_code = api.wait(name, manifest.timeout_seconds)
        if exit_code is None:
            api.kill(name)
        log, cut = api.logs_head(name, LOG_READ_LIMIT)
    except DockerError as error:
        journal.record("failed", "sandbox.run", "runtime_error", project_id=manifest.project_id)
        raise BrokerError("runtime_error", f"the sandbox could not run: {error}") from None
    finally:
        _destroy(api, name)
    duration = monotonic() - started
    outcomes: dict[str, str]
    deselected: tuple[str, ...]
    if cut:
        # Code that floods the log can push the kit's lines out of any window and leave a pair
        # of its own at the end; a log that was cut proves nothing, so the run verifies nothing.
        outcomes, deselected = dict.fromkeys(manifest.tests, "error"), ()
    else:
        outcomes, deselected = parse_outcomes(log, manifest.tests)
    journal.record("done", "sandbox.run", "ok", project_id=manifest.project_id, run=run)
    return {
        "outcomes": outcomes,
        "deselected": list(deselected),
        "log": log[-LOG_LIMIT:],
        "exit_code": exit_code,
        "timed_out": exit_code is None,
        "duration_seconds": round(duration, 3),
    }


def _destroy(api: DockerApi, name: str) -> None:
    """The sandbox is removed whatever happened (EXG-56); a failure here is retried by nobody,
    so it is attempted for both objects."""
    for remove in (api.remove, api.remove_volume):
        try:
            remove(name)
        except DockerError:
            continue
