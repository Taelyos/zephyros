"""The instance image carries the test runner of the "tests" kit (ADR 0024, sealed level)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _pins(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8").replace("\\\n", " ")
    return [" ".join(line.split()) for line in text.splitlines() if line and line[0] != "#"]


def test_the_instance_installs_the_same_pinned_wheels_as_the_kit() -> None:
    """A sealed instance runs the tests it writes with the runner that will judge them: the
    same versions and the same hashes, so neither file can move without the other."""
    instance = _pins(ROOT / "images" / "instance" / "requirements-tests.txt")
    kit = _pins(ROOT / "images" / "kit-tests" / "requirements.txt")
    assert instance == kit
    assert any(line.startswith("pytest==") for line in instance)


def test_the_instance_image_installs_them_by_hash_outside_the_venv_of_hermes() -> None:
    dockerfile = (ROOT / "images" / "instance" / "Dockerfile").read_text(encoding="utf-8")
    install = next(line for line in dockerfile.splitlines() if "pip install" in line)
    assert install.split()[1:4] == ["/usr/local/bin/python3", "-m", "pip"]
    flat = dockerfile.replace("\\\n", " ")
    assert "--require-hashes" in flat
    assert "--only-binary :all:" in flat
    assert "ENV PATH=/opt/zephyros/bin:" in dockerfile
