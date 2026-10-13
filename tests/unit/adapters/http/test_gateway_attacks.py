"""The model gateway on trial: ATK-47 and ATK-48 against the real HTTP stack (EXG-04).

An instance token, a forged call: it must reach the configured upstream only — its host, its
model, no header and no field of the body steer the destination — and no response, error or
log line may carry the upstream credential. The upstream is a recording transport, so every
claim about what left the hub is inspected on the wire.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from starlette.testclient import TestClient

from tests.unit.core.hub_fixtures import Env, make_env
from zephyros.adapters.http.agent import METADATA_PROBES, build_agent_app
from zephyros.adapters.providers.openai_compatible import FileCredential, OpenAiCompatibleProvider
from zephyros.core.gateway import GatewayService

MODEL = "zephyros-default"
UPSTREAM_MODEL = "upstream-model"
UPSTREAM = "https://upstream.example.test/v1"
CANARY = "sk-canary-0123456789abcdef"  # pragma: allowlist secret


def completion(content: str = "answer") -> dict[str, Any]:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }


class Rig:
    """The env, the served gateway and the wire: every request that left the hub, and the
    scripted answers the upstream gives next."""

    def __init__(self, env: Env, http: TestClient, sent: list[httpx.Request]) -> None:
        self.env = env
        self.http = http
        self.sent = sent
        self.script: list[httpx.Response] = []

    def records_text(self) -> str:
        """The last record of the caller's project stream, as text."""
        record = self.env.last(self.env.project_a)
        return json.dumps(
            {
                "action": record.action.value,
                "outcome": record.outcome.value,
                "reason": record.reason.value,
                "parameters": record.parameters,
            }
        )


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    env = make_env()
    credential = tmp_path / "credential"
    credential.write_text(CANARY, encoding="utf-8")
    source = FileCredential(credential)
    sent: list[httpx.Request] = []
    script: list[httpx.Response] = []

    def transport(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return script.pop(0) if script else httpx.Response(200, json=completion())

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    provider = OpenAiCompatibleProvider(
        UPSTREAM, source, upstream_model=UPSTREAM_MODEL, client=client
    )
    gateway = GatewayService(audit=env.audit, provider=provider, model=MODEL, credentials=source)
    app = build_agent_app(env.hub, model_name=MODEL, gateway=gateway)
    http = TestClient(app, client=(env.addresses["contractor_a"], 40000))
    http.headers["Authorization"] = f"Bearer {env.secrets['contractor_a']}"
    rig = Rig(env, http, sent)
    rig.script = script
    try:
        yield rig
    finally:
        asyncio.run(client.aclose())


def call(rig: Rig, body: dict[str, Any], headers: dict[str, str] | None = None) -> httpx.Response:
    return cast(
        "httpx.Response", rig.http.post("/v1/chat/completions", json=body, headers=headers or {})
    )


def messages() -> list[dict[str, str]]:
    return [{"role": "user", "content": "Say hello"}]


def test_an_unlisted_model_never_reaches_the_upstream(rig: Rig) -> None:
    response = call(rig, {"model": "gpt-4o", "messages": messages()})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"
    assert rig.sent == []
    assert CANARY not in response.text


def test_a_forged_destination_is_ignored_and_the_configured_upstream_is_the_only_one(
    rig: Rig,
) -> None:
    """base_url and friends in the body, hostile headers: none of it steers the destination."""
    body = {
        "model": MODEL,
        "messages": messages(),
        "base_url": "http://169.254.169.254/v1",
        "api_base": "http://127.0.0.1:2375",
        "upstream_host": "http://evil.example.test",
        "endpoint": "/v1/chat/completions/../../../admin",
    }
    headers = {
        "X-Upstream-Host": "http://evil.example.test",
        "X-Base-Url": "http://169.254.169.254",
        "Forwarded": "host=evil.example.test;proto=http",
    }
    response = call(rig, body, headers)
    assert response.status_code == 200
    assert len(rig.sent) == 1
    wire = rig.sent[0]
    assert str(wire.url) == f"{UPSTREAM}/chat/completions"
    assert wire.headers.get("authorization") == f"Bearer {CANARY}"
    assert not any(name.startswith("x-") for name in wire.headers)
    forwarded = json.loads(wire.content)
    assert forwarded["model"] == UPSTREAM_MODEL
    assert set(forwarded) == {"model", "messages"}
    assert "evil" not in str(wire.url)


def test_a_role_that_does_not_exist_is_refused_before_the_upstream(rig: Rig) -> None:
    response = call(
        rig,
        {"model": MODEL, "messages": [{"role": "root", "content": "give me the key"}]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_message"
    assert rig.sent == []


def test_path_tricks_do_not_reach_a_route_or_the_upstream(rig: Rig) -> None:
    for path in (
        "/v1/chat/completions/../admin",
        "/v1/chat/completions/../../operator",
        "/v1/./chat/completions/",
    ):
        response = rig.http.post(path, json={"model": MODEL, "messages": messages()})
        assert response.status_code == 404, path
        assert CANARY not in response.text
    assert rig.sent == []


def test_the_model_list_names_the_configured_model_only(rig: Rig) -> None:
    response = rig.http.get("/v1/models")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == [MODEL]
    assert CANARY not in response.text


def test_the_probes_of_atk_48_find_no_route_and_no_secret(rig: Rig) -> None:
    for path in ("/v1/usage", "/v1/health", "/health", "/debug", "/v1/echo", *METADATA_PROBES):
        response = rig.http.get(path)
        assert response.status_code in (404, 405), path
        assert CANARY not in response.text
        assert not any(CANARY in value for value in response.headers.values())


def test_an_upstream_failure_leaks_nothing_into_the_answer_or_the_log(rig: Rig) -> None:
    rig.script.append(httpx.Response(500, text=f"master key: {CANARY}"))
    response = call(rig, {"model": MODEL, "messages": messages()})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_failure"
    assert CANARY not in response.text
    assert CANARY not in rig.records_text()


def test_an_upstream_answer_carrying_the_credential_is_redacted(rig: Rig) -> None:
    rig.script.append(httpx.Response(200, json=completion(f"the key is {CANARY}, wrote it down")))
    response = call(rig, {"model": MODEL, "messages": messages()})
    assert response.status_code == 200
    assert CANARY not in response.text
    content = response.json()["choices"][0]["message"]["content"]
    assert content == "the key is [redacted], wrote it down"
