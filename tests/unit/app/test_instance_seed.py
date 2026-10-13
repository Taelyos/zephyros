"""The seed of an instance's Hermes configuration: what the runtime suite cannot afford to watch."""

from __future__ import annotations

from pathlib import Path

SEED = Path(__file__).resolve().parents[3] / "images" / "instance" / "seed.yaml"


def test_an_instance_compacts_its_conversation_before_it_grows_slow() -> None:
    """The third real run's Contractor reached 188 000 tokens a call, continuing its session from
    turn to turn; Hermes compacts past `compression.threshold_tokens` (a top-level table)."""
    lines = SEED.read_text(encoding="utf-8").splitlines()
    at = lines.index("compression:")
    assert lines[at + 1] == "  threshold_tokens: 80000"
