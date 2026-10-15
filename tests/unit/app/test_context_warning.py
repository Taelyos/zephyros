"""ADR 0033: a context below 256 000 tokens is accepted, and said where it cannot be missed: by
`zephyros config check` (which scripts/deploy.sh runs before it starts anything), and on the
standard error and in the log of the hub at every start. The evidence report says it too
(tests/unit/core/test_deliveries.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zephyros.adapters.memory.clock import FakeClock
from zephyros.adapters.memory.log_store import InMemoryLogStore
from zephyros.app.cli import main
from zephyros.app.hub import record_context
from zephyros.core.audit import HUB_STREAM, Action, AuditLog
from zephyros.core.context import AgentContext

DIGEST = "ghcr.io/example/app@sha256:" + "d" * 64


def checked(tmp_path: Path, gateway: str, capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    path = tmp_path / "z.toml"
    path.write_text(
        f'schema_version = "1.18.0"\n[images]\ninstance = "{DIGEST}"\nguard = "{DIGEST}"\n'
        f"[gateway]\n{gateway}",
        encoding="utf-8",
    )
    assert main(["config", "check", str(path)]) == 0
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_config_check_gives_the_context_of_the_agents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out, err = checked(tmp_path, "context_window = 1000000\n", capsys)
    assert (
        "the agents work with 1000000 tokens of a window of 1000000, and compact at 900000" in out
    )
    assert err == ""


def test_config_check_accepts_a_context_below_the_floor_and_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _out, err = checked(tmp_path, "context_window = 1000000\ncontext_tokens = 80000\n", capsys)
    lines = err.splitlines()
    assert lines[0] == lines[2] == "!" * 78
    assert lines[1].startswith("WARNING: the agents work with a context of 80000 tokens")
    assert "2026-10-15" in lines[1]


def log() -> AuditLog:
    return AuditLog(InMemoryLogStore(), FakeClock())


def test_the_hub_records_its_context_at_every_start(capsys: pytest.CaptureFixture[str]) -> None:
    audit = log()
    record_context(AgentContext(1_000_000, 400_000), audit)
    [record] = [r for r in audit.read(HUB_STREAM) if r.action is Action.CONTEXT_CHECK]
    assert json.loads(record.parameters) == {
        "context_window": 1_000_000,
        "context_tokens": 400_000,
        "compaction_tokens": 400_000,
        "below_floor": False,
    }
    assert capsys.readouterr().err == ""


def test_the_hub_warns_on_its_standard_error_and_in_its_log_below_the_floor(
    capsys: pytest.CaptureFixture[str],
) -> None:
    audit = log()
    record_context(AgentContext(128_000, 128_000), audit)
    [record] = [r for r in audit.read(HUB_STREAM) if r.action is Action.CONTEXT_CHECK]
    parameters = json.loads(record.parameters)
    assert parameters["below_floor"] is True
    assert "the model's context window is 128000 tokens" in parameters["warning"]
    err = capsys.readouterr().err
    assert "WARNING: " + parameters["warning"] in err
    assert "!" * 78 in err
