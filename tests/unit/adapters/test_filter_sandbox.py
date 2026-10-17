"""The sandbox runs of the filter: the archive is checked member by member, the container and
its volume live for one run only, and the route applies the strictness of the other one."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import tarfile
from collections.abc import Sequence
from typing import Any

import pytest
from starlette.testclient import TestClient

import zephyros.adapters.filter.sandbox as sandbox_module
import zephyros.adapters.filter.server as server_module
from tests.support.fake_docker import FakeDocker
from zephyros.adapters.filter.executor import Executor
from zephyros.adapters.filter.journal import MemoryJournal
from zephyros.adapters.filter.sandbox import (
    MANIFEST,
    parse_outcomes,
    parse_started,
    parse_timeouts,
    read_archive,
    run_sandbox,
    sweep_sandboxes,
)
from zephyros.adapters.filter.server import build_filter_app
from zephyros.core.broker import BrokerError, SandboxManifest
from zephyros.core.containers import Policy, sandbox_name, sandbox_spec, sandbox_volume_spec

DIGEST = "ghcr.io/example/app@sha256:" + "e" * 64
POLICY = Policy(
    guard_image=DIGEST,
    instance_image=DIGEST,
    hub_container="zephyros-hub",
    hub_port=8080,
    model="zephyros-default",
    cpus=2.0,
    memory_mib=1024,
    pids=256,
    guard_memory_mib=32,
    network_pool="10.200.0.0/16",
    network_prefix=28,
    sandbox_images=(("tests", DIGEST),),
)
PROJECT = "p_abcdef12"
TESTS = ("test_a.py::test_a", "test_b.py::test_b")
TAR = {"content-type": "application/x-tar"}


def manifest_bytes(tests: tuple[str, ...] = TESTS) -> bytes:
    return json.dumps(
        {"project_id": PROJECT, "kit": "tests", "tests": list(tests), "timeout_seconds": 60}
    ).encode()


def make_tar(members: Sequence[tuple[str, bytes]]) -> bytes:
    """A tar with the given members, in that order, duplicates included."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:") as archive:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def good_tar() -> bytes:
    return make_tar(
        [(MANIFEST, manifest_bytes()), ("test_a.py", b"def test_a():\n    assert True\n")]
    )


def refused(raw: bytes) -> str:
    with pytest.raises(BrokerError) as caught:
        read_archive(raw)
    return str(caught.value)


# ----------------------------------------------------------------------------- the archive
def test_a_good_archive_gives_the_manifest_and_a_clean_tar() -> None:
    manifest, clean = read_archive(good_tar())
    assert (manifest.project_id, manifest.kit, manifest.tests) == (PROJECT, "tests", TESTS)
    with tarfile.open(fileobj=io.BytesIO(clean), mode="r:") as archive:
        names = archive.getnames()
        file_info = archive.getmember("test_a.py")
        stored = json.loads(archive.extractfile(MANIFEST).read())  # type: ignore[union-attr]  # reason: a regular member
    assert names == ["test_a.py", MANIFEST]
    assert (file_info.mode, file_info.uid, file_info.gid, file_info.mtime) == (
        0o644,
        10000,
        10000,
        0,
    )
    digest = hashlib.sha256(b"def test_a():\n    assert True\n").hexdigest()
    assert stored == {"tests": list(TESTS), "sha256": {"test_a.py": digest}}


def test_the_kit_manifest_carries_the_digest_of_each_asked_file_only() -> None:
    """The kit holds the files it runs to what the hub sent (ADR 0017): the filter measures the
    digest of each file a test is asked from, on the bytes it received. A file no test is asked
    from, and an asked file the archive does not hold, have none."""
    raw = make_tar(
        [
            (MANIFEST, manifest_bytes()),
            ("test_a.py", b"A\n"),
            ("helper.py", b"not asked\n"),
        ]
    )
    _, clean = read_archive(raw)
    with tarfile.open(fileobj=io.BytesIO(clean), mode="r:") as archive:
        stored = json.loads(archive.extractfile(MANIFEST).read())  # type: ignore[union-attr]  # reason: a regular member
    assert stored["sha256"] == {"test_a.py": hashlib.sha256(b"A\n").hexdigest()}


def test_the_manifest_is_found_wherever_it_sits_and_directories_are_skipped() -> None:
    buffer = io.BytesIO(make_tar([("test_a.py", b"x\n"), (MANIFEST, manifest_bytes())]))
    with tarfile.open(fileobj=buffer, mode="a") as archive:
        info = tarfile.TarInfo("somedir")
        info.type = tarfile.DIRTYPE
        archive.addfile(info)
    manifest, _ = read_archive(buffer.getvalue())
    assert manifest.tests == TESTS


def test_a_body_that_is_not_a_tar_is_refused() -> None:
    assert "not a tar archive" in refused(b"not a tar at all")


def test_an_archive_without_a_manifest_is_refused() -> None:
    assert f"has no {MANIFEST}" in refused(make_tar([("test_a.py", b"x\n")]))


def test_a_second_manifest_is_refused() -> None:
    raw = make_tar([(MANIFEST, manifest_bytes()), (MANIFEST, manifest_bytes(("other.py::t",)))])
    assert f"two {MANIFEST}" in refused(raw)


@pytest.mark.parametrize(
    "broken",
    [
        b"not json",
        json.dumps(
            {"project_id": PROJECT, "kit": "tests", "tests": [], "timeout_seconds": 60}
        ).encode(),
        json.dumps(
            {
                "project_id": PROJECT,
                "kit": "tests",
                "tests": ["no-separator"],
                "timeout_seconds": 60,
            }
        ).encode(),
        json.dumps(
            {"project_id": "x", "kit": "tests", "tests": list(TESTS), "timeout_seconds": 60}
        ).encode(),
        json.dumps(
            {"project_id": PROJECT, "kit": "tests", "tests": list(TESTS), "timeout_seconds": 0}
        ).encode(),
        json.dumps(
            {"project_id": PROJECT, "kit": "tests", "tests": list(TESTS), "timeout_seconds": 86401}
        ).encode(),
    ],
)
def test_a_manifest_that_does_not_check_is_refused(broken: bytes) -> None:
    with pytest.raises(BrokerError) as caught:
        read_archive(make_tar([(MANIFEST, broken), ("test_a.py", b"x\n")]))
    assert caught.value.code in ("bad_request", "bad_json")


def test_a_run_of_hours_is_asked_for_as_the_configuration_allows() -> None:
    """ADR 0030: verification.timeout_seconds goes up to a day, and so does the manifest."""
    raw = json.dumps(
        {"project_id": PROJECT, "kit": "tests", "tests": list(TESTS), "timeout_seconds": 86400}
    ).encode()
    manifest, _clean = read_archive(make_tar([(MANIFEST, raw), ("test_a.py", b"x\n")]))
    assert manifest.timeout_seconds == 86400


def test_a_member_that_is_not_a_plain_regular_file_is_refused() -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:") as archive:
        info = tarfile.TarInfo(MANIFEST)
        info.size = len(manifest_bytes())
        archive.addfile(info, io.BytesIO(manifest_bytes()))
        link = tarfile.TarInfo("elsewhere")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    assert "only regular files" in refused(buffer.getvalue())


@pytest.mark.parametrize(
    "name",
    [
        "../escape.py",
        "/etc/passwd",
        "a/../../b.py",
        ".",
        "..",
        "café.py",
        "CONTRACT.MD`",
        "a\u202eb.py",
        "back\\slash.py",
        "line\nbreak.py",
    ],
)
def test_a_member_with_a_path_that_is_not_plain_is_refused(name: str) -> None:
    raw = make_tar([(MANIFEST, manifest_bytes()), (name, b"x\n")])
    assert "not plain" in refused(raw)


def test_a_top_level_dotfile_is_accepted() -> None:
    """A real repository carries `.gitignore` and friends: the first real run
    found this refusal — the check now allows a leading dot, and keeps refusing `.` and `..`."""
    raw = make_tar(
        [
            (MANIFEST, manifest_bytes()),
            (".gitignore", b"__pycache__/\n"),
            (".github/ci.yml", b"x\n"),
        ]
    )
    _manifest, clean = read_archive(raw)
    with tarfile.open(fileobj=io.BytesIO(clean), mode="r:") as archive:
        assert sorted(archive.getnames()) == [".github/ci.yml", ".gitignore", MANIFEST]


def test_a_name_used_twice_is_refused() -> None:
    raw = make_tar([(MANIFEST, manifest_bytes()), ("test_a.py", b"one\n"), ("test_a.py", b"two\n")])
    assert "not plain" in refused(raw)


def test_the_size_limits_are_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_module, "MAX_ARCHIVE", 512)
    assert "too large" in refused(good_tar() + b"\0" * 1024)
    monkeypatch.setattr(sandbox_module, "MAX_ARCHIVE", 64 * 1024 * 1024)
    monkeypatch.setattr(sandbox_module, "MAX_MEMBERS", 2)
    raw = make_tar([(MANIFEST, manifest_bytes()), ("a.py", b"a\n"), ("b.py", b"b\n")])
    assert "too many members" in refused(raw)


# ----------------------------------------------------------------------------- the outcomes line
NONCE = "test-nonce"


def signed(
    outcomes: dict[str, str], nonce: str = NONCE, deselected: list[str] | None = None
) -> str:
    """The two lines of an honest kit: the run line, then the signed outcome line. The signed
    payload carries both the outcomes and the deselected tests (interface 0.2.0)."""
    payload = json.dumps({"outcomes": outcomes, "deselected": deselected or []}, sort_keys=True)
    mac = hmac.new(nonce.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"ZEPHYROS-RUN {nonce}\nZEPHYROS-OUTCOMES {payload} {mac}"


def test_the_signed_pair_is_believed_and_everything_else_is_not_run() -> None:
    log = "\n".join(
        [
            signed(
                {"test_a.py::test_a": "failed", "test_b.py::test_b": "weird"},
                deselected=["test_a.py::test_other"],
            ),
            "noise, such as pytest's own report, may follow the pair",
        ]
    )
    assert parse_outcomes(log, TESTS) == (
        {"test_a.py::test_a": "failed", "test_b.py::test_b": "not_run"},
        ("test_a.py::test_other",),
    )


def test_deselected_tests_are_inside_the_signed_payload() -> None:
    """The deselected list is part of the signed payload (0.2.0), so it is authenticated like the
    outcomes: tampering with it after signing breaks the pair and nothing is believed."""
    good = signed({"test_a.py::test_a": "passed"}, deselected=["test_a.py::test_extra"])
    assert parse_outcomes(good, TESTS)[1] == ("test_a.py::test_extra",)
    payload, _, mac = good.rpartition(" ")
    tampered = payload.replace("test_extra", "test_hidden")
    assert parse_outcomes(f"{tampered} {mac}", TESTS) == (dict.fromkeys(TESTS, "error"), ())


def test_a_non_string_in_the_deselected_list_is_dropped() -> None:
    """A validly signed payload whose deselected list holds anything but a test identifier keeps
    only the identifiers: the filter never passes on a stray value."""
    body = json.dumps(
        {"outcomes": {"test_a.py::test_a": "passed"}, "deselected": ["x::y", 7, None]},
        sort_keys=True,
    )
    mac = hmac.new(NONCE.encode(), body.encode(), hashlib.sha256).hexdigest()
    log = f"ZEPHYROS-RUN {NONCE}\nZEPHYROS-OUTCOMES {body} {mac}"
    outcomes, deselected = parse_outcomes(log, ("test_a.py::test_a",))
    assert outcomes == {"test_a.py::test_a": "passed"}
    assert deselected == ("x::y",)


def test_a_line_without_a_run_line_is_not_believed() -> None:
    """ATK-22: a line with no run line of the kit is someone else's."""
    log = 'ZEPHYROS-OUTCOMES {"test_a.py::test_a": "passed"} 00ff'
    assert parse_outcomes(log, TESTS) == (dict.fromkeys(TESTS, "error"), ())


def test_a_forged_line_next_to_the_real_one_is_not_believed() -> None:
    """ATK-22: a black-box test's subprocess can reach the log and write a pair of its own — with
    the kit's own run line already there, anything but the kit's single signed line is refused."""
    log = "\n".join(
        [
            f"ZEPHYROS-RUN {NONCE}",
            signed({"test_a.py::test_a": "passed"}, nonce="evil"),
            signed({"test_a.py::test_a": "passed"}),
        ]
    )
    assert parse_outcomes(log, TESTS) == (dict.fromkeys(TESTS, "error"), ())


def test_a_log_whose_beginning_is_gone_is_not_believed() -> None:
    """ATK-22: the run line, with the nonce, is the first thing the kit prints. A log that does
    not begin with it has lost its head, and a pair left at its end is no longer anchored to
    the kit's nonce — nothing is believed."""
    forged = signed({"test_a.py::test_a": "passed"}, nonce="evil")
    log = "\n".join(["the tail of a flooded log", forged])
    assert parse_outcomes(log, TESTS) == (dict.fromkeys(TESTS, "error"), ())


def test_blank_lines_before_the_run_line_are_not_a_lost_head() -> None:
    """The head is the first line that says something: empty lines before the run line cost
    nothing, so an honest log is not refused for them."""
    log = "\n\n" + signed({"test_a.py::test_a": "passed"})
    assert parse_outcomes(log, TESTS)[0]["test_a.py::test_a"] == "passed"


def test_a_signature_that_does_not_match_is_not_believed() -> None:
    payload = '{"outcomes": {"test_a.py::test_a": "passed"}, "deselected": []}'
    log = f"ZEPHYROS-RUN {NONCE}\nZEPHYROS-OUTCOMES {payload} {'0' * 64}"
    assert parse_outcomes(log, TESTS) == (dict.fromkeys(TESTS, "error"), ())


def test_a_second_outcome_line_is_not_believed() -> None:
    """ATK-22: an honest kit prints its results once; a black-box test's subprocess can reach the
    log and print a line of its own — more than one line means nothing is believed."""
    log = "\n".join(
        [
            'ZEPHYROS-OUTCOMES {"outcomes": {"test_a.py::test_a": "passed"}, "deselected": []}',
            'ZEPHYROS-OUTCOMES {"outcomes": {"test_a.py::test_a": "passed"}, "deselected": []}',
        ]
    )
    assert parse_outcomes(log, TESTS) == (dict.fromkeys(TESTS, "error"), ())


def test_a_payload_without_an_outcomes_object_is_not_believed() -> None:
    """0.2.0: the signed payload is an object with an `outcomes` mapping; the old flat shape, or
    anything else, is refused rather than guessed at — even when its own signature is valid."""
    legacy = '{"test_a.py::test_a": "passed"}'
    mac = hmac.new(NONCE.encode(), legacy.encode(), hashlib.sha256).hexdigest()
    log = f"ZEPHYROS-RUN {NONCE}\nZEPHYROS-OUTCOMES {legacy} {mac}"
    assert parse_outcomes(log, TESTS) == (dict.fromkeys(TESTS, "error"), ())


def test_without_an_outcome_line_every_test_is_not_run() -> None:
    assert parse_outcomes("nothing to see", TESTS) == (dict.fromkeys(TESTS, "not_run"), ())


# ----------------------------------------------------------------------------- one run
class SandboxRig:
    """The filter's executor and server over the fake runtime, with the kit scripted."""

    def __init__(self, kit: Any) -> None:
        self.docker = FakeDocker()
        self.docker.add_hub()
        self.docker.kit = kit
        self.journal = MemoryJournal()
        executor = Executor(self.docker.api(), POLICY, self.journal, clock=lambda: 1_000_000)
        self.client = TestClient(build_filter_app(executor, self.journal))

    def run(self, kit: str = "tests", tests: tuple[str, ...] = TESTS) -> dict[str, Any]:
        manifest = SandboxManifest(PROJECT, kit, tests, 60)
        _, clean = read_archive(good_tar())
        return run_sandbox(
            self.docker.api(), POLICY, self.journal, manifest, clean, new_run=lambda: "0a1b2c3d"
        )


def test_one_run_creates_the_sandbox_destroys_it_and_reports_the_outcomes() -> None:
    rig = SandboxRig(lambda archive: (0, signed({"test_a.py::test_a": "passed"})))
    answer = rig.run()
    name = "zs-sbx-p_abcdef12-0a1b2c3d"
    assert answer["outcomes"] == {
        "test_a.py::test_a": "passed",
        "test_b.py::test_b": "not_run",
    }
    assert (answer["exit_code"], answer["timed_out"]) == (0, False)
    assert answer["duration_seconds"] >= 0
    assert name not in rig.docker.containers
    assert name not in rig.docker.volumes
    assert rig.journal.lines[-1] == {
        "outcome": "done",
        "op": "sandbox.run",
        "code": "ok",
        "project_id": PROJECT,
        "run": "0a1b2c3d",
    }


def test_a_flooded_log_cannot_push_the_real_lines_out_to_leave_a_forged_pair() -> None:
    """ATK-22: pytest prints a failing test's captured output after the kit's signed line, so a
    delivery whose output is echoed there can write more than the window the filter used to read
    and end with a pair of its own — a nonce of its choosing and a signature that matches it.
    With the kit's real lines pushed out of that window, the forged pair was the only one left
    and was believed. The filter now reads the whole log: both pairs are there, and nothing is."""
    real = signed({"test_a.py::test_a": "failed", "test_b.py::test_b": "failed"})
    forged = signed(dict.fromkeys(TESTS, "passed"), nonce="the-delivery-chose-this")
    flood = "x" * 250_000
    rig = SandboxRig(lambda archive: (1, "\n".join([real, flood, forged])))
    answer = rig.run()
    assert answer["outcomes"] == dict.fromkeys(TESTS, "error")
    assert answer["deselected"] == []


def test_a_log_longer_than_the_filter_reads_is_not_believed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A log past the most the filter reads has lost its beginning, where the run line is: even
    a single, validly signed pair at its end proves nothing, because the delivery may have
    written it after pushing the kit's lines out."""
    monkeypatch.setattr(sandbox_module, "LOG_READ_LIMIT", 4096)
    log = "\n".join(["y" * 5000, signed(dict.fromkeys(TESTS, "passed"))])
    rig = SandboxRig(lambda archive: (0, log))
    answer = rig.run()
    assert answer["outcomes"] == dict.fromkeys(TESTS, "error")
    assert answer["deselected"] == []


def test_a_log_whose_last_bytes_open_with_a_pair_of_the_delivery_is_not_believed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ATK-22: the delivery writes last a block of exactly one byte more than the filter reads,
    which opens with a run line of its own and a pair signed under it, and stops the judge. Were
    the log read from its end, the block would be all the filter saw: one run line, one pair,
    a valid signature — the kit's lines pushed out. The cap alone refuses it: this test
    fails if the check on the length is taken out or moved by a byte."""
    limit = 4096
    monkeypatch.setattr(sandbox_module, "LOG_READ_LIMIT", limit)
    real = signed(dict.fromkeys(TESTS, "failed"))
    forged = signed(dict.fromkeys(TESTS, "passed"), nonce="the-delivery-chose-this")
    block = forged + "\n" + "x" * (limit + 1 - len(forged.encode()) - 1)
    assert len(block.encode()) == limit + 1
    log = "\n".join([real, "y" * 3 * limit, block])
    rig = SandboxRig(lambda archive: (1, log))
    answer = rig.run()
    assert answer["outcomes"] == dict.fromkeys(TESTS, "error")
    assert answer["deselected"] == []


def test_a_log_one_byte_longer_than_what_the_filter_reads_is_not_believed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary, from above: the pair is whole in what the filter reads, and the log has one
    byte more, so it was cut — and a cut log proves nothing."""
    limit = 4096
    monkeypatch.setattr(sandbox_module, "LOG_READ_LIMIT", limit)
    pair = signed({"test_a.py::test_a": "passed"})
    log = pair + "\n" + "x" * (limit + 1 - len(pair.encode()) - 1)
    assert len(log.encode()) == limit + 1
    rig = SandboxRig(lambda archive: (0, log))
    assert rig.run()["outcomes"] == dict.fromkeys(TESTS, "error")


def test_a_log_of_exactly_what_the_filter_reads_ending_with_a_pair_is_believed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of the same boundary: one byte less, and the whole log is read."""
    limit = 4096
    monkeypatch.setattr(sandbox_module, "LOG_READ_LIMIT", limit)
    pair = signed({"test_a.py::test_a": "passed"})
    log = pair + "\n" + "x" * (limit - len(pair.encode()) - 1)
    assert len(log.encode()) == limit
    rig = SandboxRig(lambda archive: (0, log))
    assert rig.run()["outcomes"]["test_a.py::test_a"] == "passed"


def test_a_log_just_within_what_the_filter_reads_is_believed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary: the whole log, up to the limit, is read and its pair believed."""
    pair = signed({"test_a.py::test_a": "passed"})
    monkeypatch.setattr(sandbox_module, "LOG_READ_LIMIT", len(pair.encode()))
    rig = SandboxRig(lambda archive: (0, pair))
    assert rig.run()["outcomes"] == {"test_a.py::test_a": "passed", "test_b.py::test_b": "not_run"}


def test_a_run_past_its_limit_is_killed_reported_and_still_destroyed() -> None:
    rig = SandboxRig(lambda archive: (None, signed({"test_a.py::test_a": "passed"})))
    answer = rig.run(tests=("test_a.py::test_a",))
    assert answer["timed_out"] is True
    assert answer["exit_code"] is None
    assert any(c.path.endswith("/kill") for c in rig.docker.calls)
    assert not [name for name in rig.docker.containers if "zs-sbx" in name]


def test_a_kit_the_policy_does_not_know_is_refused_before_the_runtime() -> None:
    rig = SandboxRig(lambda archive: (0, ""))
    with pytest.raises(BrokerError, match="no image for the kit"):
        rig.run(kit="browser")
    assert rig.docker.mutating_calls() == []
    assert rig.journal.lines[-1]["code"] == "policy"


def test_a_runtime_failure_is_reported_and_the_objects_are_still_removed() -> None:
    rig = SandboxRig(lambda archive: (0, ""))
    rig.docker.fail_next["POST /containers/create"] = 1
    with pytest.raises(BrokerError, match="could not run"):
        rig.run(tests=("test_a.py::test_a",))
    assert rig.journal.lines[-1]["code"] == "runtime_error"
    assert not [name for name in (*rig.docker.containers, *rig.docker.volumes) if "zs-sbx" in name]


def test_a_filter_that_starts_removes_the_sandboxes_a_stopped_one_left() -> None:
    """A filter stopped during a run never reaches its `finally`: the sandbox would run on, and
    stay. The next filter removes it, and its volume, before it serves; nothing else."""
    rig = SandboxRig(lambda archive: (None, ""))
    api = rig.docker.api()
    for run in ("0a1b2c3d", "0e0f1011"):
        api.create_volume(sandbox_volume_spec(PROJECT, run))
        api.create_container(
            sandbox_name(PROJECT, run), sandbox_spec(POLICY, PROJECT, run, "tests")
        )
        api.start(sandbox_name(PROJECT, run))
    rig.docker.add_foreign(
        "container", "zs-sbx-p_abcdef12-99999999", {"org.zephyros.role": "sandbox"}
    )
    swept = sweep_sandboxes(api, rig.journal)
    assert swept == [sandbox_name(PROJECT, "0a1b2c3d"), sandbox_name(PROJECT, "0e0f1011")]
    left = [name for name in (*rig.docker.containers, *rig.docker.volumes) if "zs-sbx" in name]
    assert left == ["zs-sbx-p_abcdef12-99999999"]  # not one of the filter's
    assert [line["op"] for line in rig.journal.lines] == ["sandbox.sweep", "sandbox.sweep"]
    assert sweep_sandboxes(api, rig.journal) == []


# ----------------------------------------------------------------------------- the route
def test_the_route_runs_one_sandbox_and_answers() -> None:
    rig = SandboxRig(lambda archive: (0, signed({"test_a.py::test_a": "passed"})))
    response = rig.client.post("/sandbox", content=good_tar(), headers=TAR)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["result"]["outcomes"]["test_a.py::test_a"] == "passed"


def test_the_route_keeps_the_strictness_of_the_other_one() -> None:
    rig = SandboxRig(lambda archive: (0, ""))
    checks = [
        rig.client.post("/sandbox", content=good_tar(), headers={"content-type": "text/plain"}),
        rig.client.post("/sandbox?x=1", content=good_tar(), headers=TAR),
        rig.client.post("/sandbox", content=b"not a tar", headers=TAR),
        rig.client.post("/operation", content=good_tar(), headers=TAR),
        rig.client.post("/sandbox/x", content=good_tar(), headers=TAR),
    ]
    assert [response.status_code for response in checks] == [400, 400, 400, 400, 404]
    assert [line["code"] for line in rig.journal.lines if line["outcome"] == "refused"] == [
        "bad_request",
        "bad_request",
        "bad_request",
        "bad_request",
        "no_route",
    ]


def test_the_route_refuses_a_body_past_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_module, "MAX_ARCHIVE", 512)
    rig = SandboxRig(lambda archive: (0, ""))
    response = rig.client.post("/sandbox", content=good_tar() + b"\0" * 1024, headers=TAR)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "too_large"


# ----------------------------------------------------------------------------- time limits
def signed_line(mark: str, payload: object, nonce: str = NONCE) -> str:
    text = json.dumps(payload, sort_keys=True)
    return f"{mark}{text} {hmac.new(nonce.encode(), text.encode(), hashlib.sha256).hexdigest()}"


TIMEOUT = {"phase": "setup", "seconds": 612.3, "limit": 600}


def test_the_time_limits_a_test_reached_are_read_from_the_signed_line() -> None:
    """The kit says which tests ended on a time limit of their own, inside the signed
    payload, with the phase, the duration and the limit."""
    payload = {
        "outcomes": {"test_a.py::test_a": "error"},
        "deselected": [],
        "timeouts": {
            "test_a.py::test_a": TIMEOUT,
            "test_b.py::test_b": {"phase": "lunch", "seconds": 1},
            "test_c.py::test_c": TIMEOUT,
        },
    }
    log = f"ZEPHYROS-RUN {NONCE}\n" + signed_line("ZEPHYROS-OUTCOMES ", payload)
    assert parse_timeouts(log, TESTS) == {
        "test_a.py::test_a": {"phase": "setup", "seconds": 612.3, "limit": 600.0}
    }
    forged = log[:-1] + ("0" if log[-1] != "0" else "1")
    assert parse_timeouts(forged, TESTS) == {}
    assert parse_timeouts("nothing", TESTS) == {}


def test_the_test_under_way_is_the_last_one_the_kit_signed_as_started() -> None:
    log = "\n".join(
        [
            "",
            f"ZEPHYROS-RUN {NONCE}",
            signed_line("ZEPHYROS-STARTED ", {"test": "test_a.py::test_a", "at": 0.4}),
            signed_line("ZEPHYROS-STARTED ", {"test": "test_b.py::test_b", "at": 30.2}),
            # a subprocess can write a line, not sign one
            'ZEPHYROS-STARTED {"at": 31, "test": "test_a.py::test_a"} ' + "0" * 64,
            signed_line("ZEPHYROS-STARTED ", {"test": "not_asked.py::t", "at": 32}),
            signed_line("ZEPHYROS-STARTED ", {"test": "test_a.py::test_a", "at": -1}),
            "... the output of the test, and no outcome line: the run was stopped",
        ]
    )
    assert parse_started(log, TESTS) == {"test": "test_b.py::test_b", "at": 30.2}
    assert parse_started("garbage first\n" + log, TESTS) is None
    assert parse_started(log + f"\nZEPHYROS-RUN {NONCE}", TESTS) is None
    assert parse_started("", TESTS) is None


def test_a_run_stopped_at_its_limit_says_which_test_was_under_way() -> None:
    log = "\n".join(
        [
            f"ZEPHYROS-RUN {NONCE}",
            signed_line("ZEPHYROS-STARTED ", {"test": "test_a.py::test_a", "at": 0.5}),
        ]
    )
    answer = SandboxRig(lambda archive: (None, log)).run()
    assert answer["timed_out"] is True
    assert answer["started"] == {"test": "test_a.py::test_a", "at": 0.5}
    assert answer["timeouts"] == {}
    assert set(answer["outcomes"].values()) == {"not_run"}


def test_a_run_that_ended_carries_the_time_limits_of_its_tests() -> None:
    payload = {
        "outcomes": {"test_a.py::test_a": "error", "test_b.py::test_b": "passed"},
        "deselected": [],
        "timeouts": {"test_a.py::test_a": TIMEOUT},
    }
    log = f"ZEPHYROS-RUN {NONCE}\n" + signed_line("ZEPHYROS-OUTCOMES ", payload)
    answer = SandboxRig(lambda archive: (1, log)).run()
    assert answer["timeouts"] == {
        "test_a.py::test_a": {"phase": "setup", "seconds": 612.3, "limit": 600.0}
    }
    assert answer["started"] is None


def test_a_started_line_after_what_pytest_wrote_on_the_same_line_is_still_read() -> None:
    """In its classic style pytest writes the name of the file with no line end: the kit begins
    its line with a newline, and a line that does not begin with the mark is not read."""
    started = signed_line("ZEPHYROS-STARTED ", {"test": "test_a.py::test_a", "at": 0.5})
    log = f"ZEPHYROS-RUN {NONCE}\ntest_a.py \n{started}\n"
    assert parse_started(log, TESTS) == {"test": "test_a.py::test_a", "at": 0.5}
    assert parse_started(f"ZEPHYROS-RUN {NONCE}\ntest_a.py {started}\n", TESTS) is None
