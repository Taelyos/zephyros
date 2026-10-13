"""The operator side of the command line."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests.unit.core.hub_fixtures import Env, make_env
from zephyros.adapters.http.operator import build_operator_app
from zephyros.app.cli import main, run_call


@pytest.fixture
def env() -> Env:
    return make_env()


def operator_client(env: Env) -> TestClient:
    return TestClient(
        build_operator_app(env.hub, port=8081),
        base_url="http://127.0.0.1:8081",
        client=("172.18.0.1", 43000),
    )


def test_a_call_prints_the_answer_and_returns_zero(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run_call(
        operator_client(env), "http://127.0.0.1:8081", env.secrets["operator"], "whoami", {}
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["data"]["kind"] == "operator"


def test_a_refused_call_returns_one(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    code = run_call(
        operator_client(env), "http://127.0.0.1:8081", env.secrets["operator"], "log.delete", {}
    )
    assert code == 1
    assert json.loads(capsys.readouterr().out)["error"]["kind"] == "unknown_tool"


def test_a_wrong_token_returns_one(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    code = run_call(
        operator_client(env), "http://127.0.0.1:8081", "zph_h_" + "A" * 43, "whoami", {}
    )
    assert code == 1
    assert json.loads(capsys.readouterr().out) == {"error": "unauthorized"}


def test_hub_call_reports_an_unreadable_token_file_or_arguments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["hub", "call", "whoami", "--token-file", str(tmp_path / "absent")]) == 2
    token = tmp_path / "t"
    token.write_text("zph_h_x\n", encoding="ascii")
    assert main(["hub", "call", "whoami", "--args", "{not json", "--token-file", str(token)]) == 2
    assert "cannot read" in capsys.readouterr().err
