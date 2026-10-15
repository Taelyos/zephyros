"""ADR 0033: a compaction point below 256 000 tokens, or a real model whose window the file does
not declare, is accepted and said where it cannot be missed: by `zephyros config check` (which
scripts/deploy.sh runs before it starts anything), and on the standard error and in the log of
the hub at every start. The evidence report says the floor too
(tests/unit/core/test_deliveries.py). The gateway of the hub takes conversations as large as the
window (tests/unit/adapters/http/test_gateway_size.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zephyros.adapters.memory.clock import FakeClock
from zephyros.adapters.memory.instance_control import FakeInstanceControl
from zephyros.adapters.memory.log_store import InMemoryLogStore
from zephyros.adapters.sqlite.database import Database
from zephyros.app.cli import main
from zephyros.app.hub import build_hub, record_context
from zephyros.core.audit import HUB_STREAM, Action, AuditLog
from zephyros.core.config import CONFIG_SCHEMA_VERSION, parse_config
from zephyros.core.gateway import GatewayLimits

DIGEST = "ghcr.io/example/app@sha256:" + "d" * 64
REAL = (
    'provider = "openai-compatible"\nupstream_url = "https://provider.example.test/v1"\n'
    'credential_file = "/t/key"\nmodels = ["zephyros-default"]\n'
)


def document(gateway: str) -> str:
    return (
        f'schema_version = "{CONFIG_SCHEMA_VERSION}"\n[images]\ninstance = "{DIGEST}"\n'
        f'guard = "{DIGEST}"\n[gateway]\n{gateway}'
    )


def checked(tmp_path: Path, gateway: str, capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    path = tmp_path / "z.toml"
    path.write_text(document(gateway), encoding="utf-8")
    assert main(["config", "check", str(path)]) == 0
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_config_check_gives_the_context_and_says_nothing_of_a_declared_large_window(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out, err = checked(tmp_path, REAL + "context_window = 1000000\n", capsys)
    assert (
        "the agents work with 1000000 tokens of a window of 1000000, and compact at 900000" in out
    )
    assert err == ""


def test_config_check_accepts_a_context_below_the_floor_and_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _out, err = checked(
        tmp_path, REAL + "context_window = 1000000\ncontext_tokens = 80000\n", capsys
    )
    lines = err.splitlines()
    assert lines[0] == lines[-1] == "!" * 78
    [warning] = lines[1:-1]
    assert warning.startswith("WARNING: the agents compact at 80000 tokens")
    assert "a long task loses the thread of its work" in warning


def test_config_check_warns_of_a_window_not_declared_for_a_real_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _out, err = checked(tmp_path, REAL, capsys)
    assert "WARNING: gateway.context_window is not declared" in err
    assert "WARNING: the agents compact at 224000 tokens" in err


def test_config_check_says_nothing_with_the_stub(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _out, err = checked(tmp_path, "simulation = true\n", capsys)
    assert err == ""


def log() -> AuditLog:
    return AuditLog(InMemoryLogStore(), FakeClock())


REAL_TABLE: dict[str, object] = {
    "provider": "openai-compatible",
    "upstream_url": "https://provider.example.test/v1",
    "credential_file": "/t/key",
    "models": ["zephyros-default"],
}


def test_the_hub_records_its_context_at_every_start(capsys: pytest.CaptureFixture[str]) -> None:
    audit = log()
    gateway = parse_config(
        {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "images": {"instance": DIGEST, "guard": DIGEST},
            "gateway": {**REAL_TABLE, "context_window": 1_000_000, "context_tokens": 400_000},
        }
    ).gateway
    record_context(gateway, audit)
    [record] = [r for r in audit.read(HUB_STREAM) if r.action is Action.CONTEXT_CHECK]
    assert json.loads(record.parameters) == {
        "context_window": 1_000_000,
        "context_declared": True,
        "context_tokens": 400_000,
        "compaction_tokens": 400_000,
        "below_floor": False,
    }
    assert capsys.readouterr().err == ""


def test_the_hub_warns_on_its_standard_error_and_in_its_log(
    capsys: pytest.CaptureFixture[str],
) -> None:
    audit = log()
    gateway = parse_config(
        {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "images": {"instance": DIGEST, "guard": DIGEST},
            "gateway": REAL_TABLE,
        }
    ).gateway
    record_context(gateway, audit)
    [record] = [r for r in audit.read(HUB_STREAM) if r.action is Action.CONTEXT_CHECK]
    parameters = json.loads(record.parameters)
    assert parameters["below_floor"] is True
    assert parameters["context_declared"] is False
    assert len(parameters["warnings"]) == 2
    err = capsys.readouterr().err
    assert all("WARNING: " + warning in err for warning in parameters["warnings"])
    assert "!" * 78 in err


def test_the_gateway_of_the_hub_takes_conversations_as_large_as_the_window() -> None:
    """ADR 0033: the limits of the gateway follow gateway.context_window."""
    runtime = build_hub(
        Database(":memory:"), FakeInstanceControl(), FakeClock(), context_window=1_000_000
    )
    assert runtime.gateway.limits == GatewayLimits.for_window(1_000_000)
    default = build_hub(Database(":memory:"), FakeInstanceControl(), FakeClock())
    assert default.gateway.limits == GatewayLimits.for_window(256_000)
