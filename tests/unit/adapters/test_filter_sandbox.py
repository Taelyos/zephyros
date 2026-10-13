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
    read_archive,
    run_sandbox,
)
from zephyros.adapters.filter.server import build_filter_app
from zephyros.core.broker import BrokerError, SandboxManifest
from zephyros.core.containers import Policy

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
    assert stored == {"tests": list(TESTS)}


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
    ],
)
def test_a_manifest_that_does_not_check_is_refused(broken: bytes) -> None:
    with pytest.raises(BrokerError) as caught:
        read_archive(make_tar([(MANIFEST, broken), ("test_a.py", b"x\n")]))
    assert caught.value.code in ("bad_request", "bad_json")


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
    """A real repository carries `.gitignore` and friends: the first real run (steam-watch)
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


def signed(outcomes: dict[str, str], nonce: str = NONCE) -> str:
    """The two lines of an honest kit: the run line, then the signed outcome line."""
    payload = json.dumps(outcomes, sort_keys=True)
    mac = hmac.new(nonce.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"ZEPHYROS-RUN {nonce}\nZEPHYROS-OUTCOMES {payload} {mac}"


def test_the_signed_pair_is_believed_and_everything_else_is_not_run() -> None:
    log = "\n".join(
        [
            "noise",
            signed({"test_a.py::test_a": "failed", "test_b.py::test_b": "weird"}),
        ]
    )
    assert parse_outcomes(log, TESTS) == {
        "test_a.py::test_a": "failed",
        "test_b.py::test_b": "not_run",
    }


def test_a_line_without_a_run_line_is_not_believed() -> None:
    """ATK-22: a line with no run line of the kit is someone else's."""
    log = 'ZEPHYROS-OUTCOMES {"test_a.py::test_a": "passed"} 00ff'
    assert parse_outcomes(log, TESTS) == dict.fromkeys(TESTS, "error")


def test_a_forged_line_next_to_the_real_one_is_not_believed() -> None:
    """ATK-22: the delivered code can reach the log and write a pair of its own — with the
    kit's own run line already there, anything but the kit's single signed line is refused."""
    log = "\n".join(
        [
            f"ZEPHYROS-RUN {NONCE}",
            signed({"test_a.py::test_a": "passed"}, nonce="evil"),
            signed({"test_a.py::test_a": "passed"}),
        ]
    )
    assert parse_outcomes(log, TESTS) == dict.fromkeys(TESTS, "error")


def test_a_signature_that_does_not_match_is_not_believed() -> None:
    payload = '{"test_a.py::test_a": "passed"}'
    log = f"ZEPHYROS-RUN {NONCE}\nZEPHYROS-OUTCOMES {payload} {'0' * 64}"
    assert parse_outcomes(log, TESTS) == dict.fromkeys(TESTS, "error")


def test_a_second_outcome_line_is_not_believed() -> None:
    """ATK-22: an honest kit prints its results once; a delivered test can reach the log and
    print a line of its own — more than one line means nothing is believed."""
    log = "\n".join(
        [
            'ZEPHYROS-OUTCOMES {"test_a.py::test_a": "passed"}',
            'ZEPHYROS-OUTCOMES {"test_a.py::test_a": "passed"}',
        ]
    )
    assert parse_outcomes(log, TESTS) == dict.fromkeys(TESTS, "error")


def test_without_an_outcome_line_every_test_is_not_run() -> None:
    assert parse_outcomes("nothing to see", TESTS) == dict.fromkeys(TESTS, "not_run")


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
