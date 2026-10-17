"""The hub's sandbox runner: what it packs, what it refuses, how it reads the filter's answer."""

from __future__ import annotations

import io
import json
import tarfile
from typing import Any, cast

import httpx
import pytest

from zephyros.adapters.filter.client import FilterClient
from zephyros.adapters.filter.sandbox import MANIFEST
from zephyros.adapters.sandbox_runner import SandboxRunner, pack
from zephyros.core.broker import BrokerError
from zephyros.core.ports.verification import (
    INTERFACE_VERSION,
    Outcome,
    TimeLimit,
    VerificationError,
    VerificationRequest,
)

FILES = {"test_a.py": b"def test_a():\n    assert True\n"}
TESTS = ("test_a.py::test_a", "test_b.py::test_b")


def request_(tests: tuple[str, ...] = TESTS, kit: str = "tests") -> VerificationRequest:
    return VerificationRequest("p_abcdef12", kit, FILES, tests)


def runner_for(handler: Any) -> SandboxRunner:
    client = FilterClient(httpx.Client(transport=httpx.MockTransport(handler), base_url="http://f"))
    return SandboxRunner(client, ["tests"])


def test_pack_puts_the_manifest_first_and_the_files_sorted() -> None:
    raw = pack(request_())
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        names = archive.getnames()
        manifest = json.loads(archive.extractfile(MANIFEST).read())  # type: ignore[union-attr]  # reason: a regular member
    assert names == [MANIFEST, "test_a.py"]
    assert manifest == {
        "project_id": "p_abcdef12",
        "kit": "tests",
        "tests": list(TESTS),
        "timeout_seconds": 300,
    }


def test_pack_refuses_a_file_named_like_the_manifest() -> None:
    request = VerificationRequest("p_abcdef12", "tests", {MANIFEST: b"x\n"}, TESTS)
    with pytest.raises(VerificationError, match="reserves"):
        pack(request)


def test_the_runner_names_its_interface_and_its_kits() -> None:
    runner = runner_for(lambda request: httpx.Response(200, json={"ok": True, "result": {}}))
    assert runner.interface_version == INTERFACE_VERSION
    assert runner.kits == frozenset({"tests"})


def test_a_kit_the_runner_does_not_run_is_refused_without_a_call() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    runner = runner_for(handler)
    with pytest.raises(VerificationError, match="does not run the kit"):
        runner.run(request_(kit="browser"))
    assert seen == []


def manifest_of(raw: bytes) -> dict[str, Any]:
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        member = archive.extractfile(MANIFEST)
    assert member is not None  # reason: the manifest is a regular member of every archive
    return cast("dict[str, Any]", json.loads(member.read()))


def test_the_answer_gives_one_outcome_per_test_asked_for() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        asked = manifest_of(request.content)["tests"]
        outcomes = {
            test: ("weird" if test == "test_b.py::test_b" else "passed")
            if "canary" not in test
            else "failed"
            for test in asked
        }
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "outcomes": outcomes,
                    "log": "the kit ran",
                    "timed_out": False,
                    "duration_seconds": 1.5,
                },
            },
        )

    result = runner_for(handler).run(request_())
    assert dict(result.outcomes) == {
        "test_a.py::test_a": Outcome.PASSED,
        "test_b.py::test_b": Outcome.NOT_RUN,
    }
    assert result.log == "the kit ran"
    assert result.duration_seconds == 1.5


def test_every_run_carries_canaries_that_must_fail_and_they_stay_internal() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        asked = manifest_of(request.content)["tests"]
        outcomes = {
            test: (
                "failed"
                if "canary" in test
                else ("passed" if test == "test_a.py::test_a" else "not_run")
            )
            for test in asked
        }
        return httpx.Response(
            200, json={"ok": True, "result": {"outcomes": outcomes, "timed_out": False}}
        )

    result = runner_for(handler).run(request_())
    assert dict(result.outcomes) == {
        "test_a.py::test_a": Outcome.PASSED,
        "test_b.py::test_b": Outcome.NOT_RUN,
    }
    (sent,) = seen
    asked = manifest_of(sent.content)["tests"]
    assert len(asked) == len(TESTS) + 2
    assert sum(1 for test in asked if "canary" in test) == 2
    with tarfile.open(fileobj=io.BytesIO(sent.content), mode="r:") as archive:
        cloned = [name for name in archive.getnames() if "canary" in name]
    assert len(cloned) == 2


def test_a_run_whose_canary_did_not_fail_is_not_trusted() -> None:
    """ATK-22: a conftest that marks everything passed, an early exit, a monkeypatched
    framework — the canaries tell the forgery from a genuine run."""

    def handler(request: httpx.Request) -> httpx.Response:
        asked = manifest_of(request.content)["tests"]
        outcomes = dict.fromkeys(asked, "passed")
        return httpx.Response(
            200, json={"ok": True, "result": {"outcomes": outcomes, "timed_out": False}}
        )

    with pytest.raises(VerificationError, match="cannot be trusted"):
        runner_for(handler).run(request_())

    def absent(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": True, "result": {"outcomes": {}, "timed_out": False}}
        )

    with pytest.raises(VerificationError, match="cannot be trusted"):
        runner_for(absent).run(request_())


def test_a_run_past_its_limit_says_so_in_the_log() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "result": {"outcomes": {}, "log": "halfway", "timed_out": True}},
        )

    result = runner_for(handler).run(request_(tests=("test_a.py::test_a",)))
    assert result.log.endswith("[the run reached its time limit and was stopped]")


def test_a_run_past_its_limit_verifies_nothing_even_with_forged_outcomes() -> None:
    """ATK-22: the delivered code can print a results line before the kill; a run stopped at
    its limit proves nothing, so the outcomes it claims are not believed."""
    forged = {
        "test_a.py::test_a": "passed",
        "test_zephyros_canary_x.py::test_zephyros_canary_x": "failed",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "result": {"outcomes": forged, "log": "forged", "timed_out": True}},
        )

    result = runner_for(handler).run(request_(tests=("test_a.py::test_a",)))
    assert result.outcomes["test_a.py::test_a"] is Outcome.NOT_RUN
    assert result.deselected == ()


def test_the_deselected_tests_reported_by_the_filter_reach_the_result() -> None:
    """0.2.0: the kit lists the tests it saw in the asked files but did not run for want of a
    criterion; the runner carries them through so the report can say they never ran (A2)."""

    def handler(request: httpx.Request) -> httpx.Response:
        asked = manifest_of(request.content)["tests"]
        outcomes = {test: ("failed" if "canary" in test else "passed") for test in asked}
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "outcomes": outcomes,
                    "deselected": ["test_a.py::test_unnamed", 7],
                    "timed_out": False,
                },
            },
        )

    result = runner_for(handler).run(request_())
    # Only well-formed identifiers survive.
    assert result.deselected == ("test_a.py::test_unnamed",)


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(200, json={"ok": False, "error": {"code": "policy"}}),
        lambda request: httpx.Response(500, text="broken"),
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("no filter")),
    ],
)
def test_a_filter_that_does_not_answer_ok_is_a_verification_error(handler: Any) -> None:
    with pytest.raises(VerificationError, match="the sandbox did not run"):
        runner_for(handler).run(request_(tests=("test_a.py::test_a",)))


def test_the_runner_sends_the_archive_to_the_sandbox_route() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        asked = manifest_of(request.content)["tests"]
        outcomes = {test: ("failed" if "canary" in test else "passed") for test in asked}
        return httpx.Response(
            200, json={"ok": True, "result": {"outcomes": outcomes, "timed_out": False}}
        )

    runner_for(handler).run(
        VerificationRequest("p_abcdef12", "tests", FILES, ("test_a.py::test_a",))
    )
    (sent,) = seen
    assert sent.url.path == "/sandbox"
    assert sent.headers["content-type"] == "application/x-tar"
    with tarfile.open(fileobj=io.BytesIO(sent.content), mode="r:") as archive:
        names = archive.getnames()
    assert names[0] == MANIFEST
    assert "test_a.py" in names
    assert len([name for name in names if "canary" in name]) == 2


def test_a_broker_error_of_the_route_keeps_its_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "error": {"code": "bad_request"}})

    client = FilterClient(httpx.Client(transport=httpx.MockTransport(handler), base_url="http://f"))
    with pytest.raises(BrokerError) as caught:
        client.run_sandbox(b"", 60)
    assert caught.value.code == "bad_request"


def _answering(**result: Any) -> Any:
    """A filter that fails the canaries as an honest kit does, and answers `result` besides."""

    def handler(request: httpx.Request) -> httpx.Response:
        asked = manifest_of(request.content)["tests"]
        outcomes = {test: "failed" if "canary" in test else "error" for test in asked}
        return httpx.Response(200, json={"ok": True, "result": {"outcomes": outcomes, **result}})

    return handler


def test_a_test_that_reached_its_own_time_limit_says_so() -> None:
    """A test over its limit used to read `error`, with the cause only in the raw output."""
    handler = _answering(
        timed_out=False,
        duration_seconds=700.0,
        timeouts={
            "test_a.py::test_a": {"phase": "setup", "seconds": 612.3, "limit": 600.0},
            "test_zephyros_canary_x.py::test_x": {"phase": "call", "seconds": 1.0},
            "test_b.py::test_b": "not a cause",
        },
    )
    result = runner_for(handler).run(request_())
    assert result.outcomes["test_a.py::test_a"] is Outcome.ERROR  # the gate counts it so
    assert dict(result.timeouts) == {"test_a.py::test_a": TimeLimit("setup", 612.3, 600.0)}
    assert result.stopped_at is None


def test_a_run_stopped_at_its_limit_names_the_test_under_way() -> None:
    handler = _answering(
        timed_out=True,
        duration_seconds=301.0,
        started={"test": "test_b.py::test_b", "at": 1.5},
    )
    result = runner_for(handler).run(request_())
    assert set(result.outcomes.values()) == {Outcome.NOT_RUN}
    assert result.stopped_at == 300
    assert dict(result.timeouts) == {"test_b.py::test_b": TimeLimit("run", 299.5)}
    edge = _answering(timed_out=True, duration_seconds=10.152, started={"test": TESTS[0], "at": 0})
    assert runner_for(edge).run(request_()).timeouts[TESTS[0]].seconds == 10.152


def test_a_run_stopped_with_no_test_under_way_names_none() -> None:
    for started in (None, {"test": "test_zephyros_canary_x.py::test_x", "at": 1.0}):
        result = runner_for(_answering(timed_out=True, started=started)).run(request_())
        assert result.stopped_at == 300
        assert dict(result.timeouts) == {}
