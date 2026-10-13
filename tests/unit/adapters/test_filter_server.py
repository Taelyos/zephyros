"""The listener of the container filter: one strict route, and nothing shaped like the runtime."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.support.fake_docker import FakeDocker
from zephyros.adapters.filter.executor import Executor
from zephyros.adapters.filter.journal import MemoryJournal
from zephyros.adapters.filter.server import build_filter_app
from zephyros.core.broker import MAX_BODY
from zephyros.core.containers import Policy

PROJECT = "p_abcdef12"
SUBNET = "10.200.0.16/28"
TOKEN = "zph_c_" + "T" * 43
POLICY = Policy(
    guard_image="ghcr.io/example/guard@sha256:" + "a" * 64,
    instance_image="ghcr.io/example/instance@sha256:" + "b" * 64,
    hub_container="zephyros-hub",
    hub_port=8080,
    model="zephyros-default",
    cpus=2.0,
    memory_mib=1024,
    pids=256,
    guard_memory_mib=32,
    network_pool="10.200.0.0/16",
    network_prefix=28,
)
JSON = {"content-type": "application/json"}
CREATE = {"op": "network.create", "project_id": PROJECT, "side": "owner", "subnet": SUBNET}


class Rig:
    def __init__(self) -> None:
        self.docker = FakeDocker()
        self.docker.add_hub()
        self.journal = MemoryJournal()
        executor = Executor(self.docker.api(), POLICY, self.journal, clock=lambda: 1_000_000)
        self.client = TestClient(build_filter_app(executor, self.journal))

    def post(self, body: Any = None, *, raw: bytes | None = None, **options: Any) -> Any:
        headers = {**JSON, **options.pop("headers", {})}
        content = raw if raw is not None else json.dumps(body).encode()
        return self.client.post("/operation", content=content, headers=headers, **options)

    def codes(self) -> list[str]:
        return [line["code"] for line in self.journal.lines if line["outcome"] == "refused"]

    @property
    def untouched(self) -> bool:
        return not self.docker.calls


@pytest.fixture
def rig() -> Rig:
    return Rig()


# ----------------------------------------------------------------------------- the permitted calls
def test_a_permitted_request_is_done_and_answered(rig: Rig) -> None:
    response = rig.post(CREATE)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "result": {"created": True}}
    assert rig.post({"op": "container.list"}).json() == {"ok": True, "result": {"containers": []}}


@pytest.mark.parametrize(
    ("code", "status"),
    [("conflict", 409), ("not_found", 404), ("policy", 400)],
)
def test_the_executors_refusals_keep_their_code_and_get_a_matching_status(
    rig: Rig, code: str, status: int
) -> None:
    attempts = {
        "conflict": [CREATE, {**CREATE, "subnet": "10.200.0.32/28"}],
        "not_found": [{"op": "hub.connect", "project_id": PROJECT, "side": "contractor"}],
        "policy": [{**CREATE, "subnet": "172.17.0.0/28"}],
    }[code]
    response = None
    for body in attempts:
        response = rig.post(body)
    assert response is not None
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_a_runtime_failure_is_a_502_without_the_runtimes_text(rig: Rig) -> None:
    rig.docker.fail_next["POST /networks/create"] = 1
    response = rig.post(CREATE)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "runtime_error"


# ------------------------------------------------------------ shapes of the runtime
@pytest.mark.parametrize(
    "path",
    [
        "/containers/create",
        "/containers/json",
        "/v1.45/containers/create",
        "/v1.45/networks/create",
        "/networks/create",
        "/volumes/create",
        "/images/create",
        "/build",
        "/commit",
        "/exec/abc/start",
        "/containers/zs-guard-p_abcdef12-owner/exec",
        "/containers/zs-guard-p_abcdef12-owner/kill",
        "/_ping",
        "/version",
        "/info",
        "/events",
        "/operation/",
        "/operation/extra",
        "//operation",
        "/Operation",
        "/",
        "/var/run/docker.sock",
    ],
)
@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
def test_a_request_shaped_like_a_call_to_the_runtime_finds_no_route_and_reaches_nothing(
    rig: Rig, path: str, method: str
) -> None:
    response = rig.client.request(
        method,
        path,
        content=json.dumps({"Image": "alpine", "HostConfig": {"Privileged": True}}),
        headers=JSON,
    )
    assert response.status_code in (404, 405)
    assert rig.untouched
    assert "no_route" in rig.codes() or response.status_code == 405


@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE"])
def test_the_one_route_takes_post_only(rig: Rig, method: str) -> None:
    response = rig.client.request(method, "/operation", content=json.dumps(CREATE), headers=JSON)
    assert response.status_code == 404
    assert rig.untouched


@pytest.mark.parametrize(
    "path",
    ["/%6Fperation", "/operation%2F", "/%2Foperation", "/operation%00", "/operation;x=1"],
)
def test_a_path_that_is_not_written_plainly_is_refused(rig: Rig, path: str) -> None:
    response = rig.client.post(path, content=json.dumps(CREATE), headers=JSON)
    assert response.status_code in (400, 404)
    assert rig.untouched


@pytest.mark.parametrize("query", ["?x=1", "?op=container.list", "?force=true"])
def test_a_query_string_is_refused(rig: Rig, query: str) -> None:
    response = rig.client.post("/operation" + query, content=json.dumps(CREATE), headers=JSON)
    assert response.status_code == 400
    assert rig.untouched


@pytest.mark.parametrize(
    "header",
    [
        "Transfer-Encoding",
        "Expect",
        "Upgrade",
        "Content-Encoding",
        "X-HTTP-Method-Override",
        "X-Method-Override",
        "X-HTTP-Method",
        "Forwarded",
        "X-Forwarded-For",
    ],
)
def test_headers_that_change_how_a_request_is_read_are_refused(rig: Rig, header: str) -> None:
    response = rig.post(CREATE, headers={header: "DELETE"})
    assert response.status_code == 400
    assert rig.untouched


def test_a_chunked_body_is_refused(rig: Rig) -> None:
    def chunks() -> Iterator[bytes]:
        yield json.dumps(CREATE).encode()

    response = rig.client.post("/operation", content=chunks(), headers=JSON)
    assert response.status_code == 400
    assert rig.untouched


@pytest.mark.parametrize("kind", ["text/plain", "application/x-www-form-urlencoded", "", "JSON"])
def test_only_application_json_is_accepted(rig: Rig, kind: str) -> None:
    response = rig.client.post(
        "/operation", content=json.dumps(CREATE), headers={"content-type": kind}
    )
    assert response.status_code == 400
    assert rig.untouched


@pytest.mark.parametrize("length", ["abc", "-1", "1e3", ""])
def test_the_body_must_declare_a_plain_length(rig: Rig, length: str) -> None:
    response = rig.post(CREATE, headers={"content-length": length})
    assert response.status_code in (400, 422)
    assert rig.untouched


def test_a_body_that_is_too_large_is_refused_before_it_is_read(rig: Rig) -> None:
    response = rig.post(raw=b" " * (MAX_BODY + 1))
    assert response.status_code == 413
    assert rig.untouched
    declared = rig.post(CREATE, headers={"content-length": str(MAX_BODY + 1)})
    assert declared.status_code == 413
    assert rig.untouched


# ----------------------------------------------------------------------------- bodies
@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"", "bad_json"),
        (b"not json", "bad_json"),
        (b"\xff\xfe", "bad_json"),
        (b"[]", "bad_request"),
        (b'"op"', "bad_request"),
        (b"null", "bad_request"),
        (b'{"op": "container.list", "op": "events"}', "duplicate_key"),
        (b'{"op": "events", "since": NaN}', "bad_json"),
        (b'{"op": "events", "since": Infinity}', "bad_json"),
        (b"{}", "unknown_operation"),
        (b'{"op": "container.exec"}', "unknown_operation"),
        (b'{"op": "containers/create"}', "unknown_operation"),
        (b'{"op": ["container.list"]}', "unknown_operation"),
    ],
)
def test_a_body_that_is_not_one_permitted_request_is_refused_with_a_stable_code(
    rig: Rig, raw: bytes, code: str
) -> None:
    response = rig.post(raw=raw)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == code
    assert rig.untouched
    assert code in rig.codes()


@pytest.mark.parametrize(
    "extra",
    [
        {"Privileged": True},
        {"image": "alpine"},
        {"Image": "alpine"},
        {"HostConfig": {"Binds": ["/:/host"]}},
        {"cmd": ["sh"]},
        {"network_mode": "host"},
        {"labels": {"org.zephyros.managed": "true"}},
        {"name": "zs-guard-other"},
    ],
)
def test_a_field_that_the_kind_does_not_have_is_refused(rig: Rig, extra: dict[str, Any]) -> None:
    response = rig.post({**CREATE, **extra})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert rig.untouched


def test_the_journal_never_holds_a_token_or_a_prompt_from_a_refused_request(rig: Rig) -> None:
    body = {
        "op": "turn.start",
        "project_id": PROJECT,
        "side": "owner",
        "token": TOKEN,
        "prompt": "top secret",
        "Privileged": True,
    }
    assert rig.post(body).status_code == 400
    text = str(rig.journal.lines)
    assert TOKEN not in text
    assert "top secret" not in text
    assert rig.untouched
