"""US-E11-05 (CTR-10): the pin checker accepts pinned references and rejects the others."""

from __future__ import annotations

from pathlib import Path

import pytest
from tools import check_pins

DIGEST = "sha256:" + "a" * 64
SHA = "b" * 40


def _write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


LOCK_OK = f"""
version = 1

[[package]]
name = "zephyros"
version = "0.0.0"
source = {{ editable = "." }}

[[package]]
name = "left-pad"
version = "1.0.0"
source = {{ registry = "https://pypi.org/simple" }}
sdist = {{ url = "https://example.invalid/a.tar.gz", hash = "{DIGEST}", size = 1 }}
wheels = [
    {{ url = "https://example.invalid/a.whl", hash = "{DIGEST}", size = 1 }},
]
"""


class TestLock:
    def test_accepts_registry_packages_with_hashes(self, tmp_path: Path) -> None:
        lock = _write(tmp_path, "uv.lock", LOCK_OK)
        assert check_pins.check_lock(lock) == []

    def test_rejects_missing_lock(self, tmp_path: Path) -> None:
        assert check_pins.check_lock(tmp_path / "uv.lock") != []

    def test_rejects_a_wheel_without_hash(self, tmp_path: Path) -> None:
        text = LOCK_OK.replace(f', hash = "{DIGEST}", size = 1 }},\n]', ", size = 1 },\n]")
        lock = _write(tmp_path, "uv.lock", text)
        assert any("no sha256 hash" in f for f in check_pins.check_lock(lock))

    def test_rejects_a_git_source(self, tmp_path: Path) -> None:
        text = (
            LOCK_OK
            + '\n[[package]]\nname = "evil"\nversion = "1"\nsource = { git = "https://x" }\n'
        )
        lock = _write(tmp_path, "uv.lock", text)
        assert any("not a registry" in f for f in check_pins.check_lock(lock))

    def test_rejects_a_package_without_artifact(self, tmp_path: Path) -> None:
        text = LOCK_OK + (
            '\n[[package]]\nname = "bare"\nversion = "1"\n'
            'source = { registry = "https://pypi.org/simple" }\n'
        )
        lock = _write(tmp_path, "uv.lock", text)
        assert any("no artifact" in f for f in check_pins.check_lock(lock))


class TestBuildSystem:
    def _pyproject(self, tmp_path: Path, requires: str) -> Path:
        return _write(tmp_path, "pyproject.toml", f"[build-system]\nrequires = {requires}\n")

    def test_accepts_exact_pins(self, tmp_path: Path) -> None:
        path = self._pyproject(tmp_path, '["uv_build==0.12.24", "other-thing==1.0.post1"]')
        assert check_pins.check_build_system(path) == []

    @pytest.mark.parametrize(
        "requires",
        ['["hatchling"]', '["uv_build>=0.12"]', '["uv_build~=0.12.24"]', '["uv_build==0.12.*"]'],
    )
    def test_rejects_a_requirement_that_is_not_an_exact_pin(
        self, tmp_path: Path, requires: str
    ) -> None:
        path = self._pyproject(tmp_path, requires)
        assert len(check_pins.check_build_system(path)) == 1

    def test_rejects_a_missing_pyproject(self, tmp_path: Path) -> None:
        findings = check_pins.check_build_system(tmp_path / "pyproject.toml")
        assert any("is missing" in f for f in findings)

    def test_rejects_a_missing_build_system(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n")
        assert any("no build requirement" in f for f in check_pins.check_build_system(path))


class TestRequirements:
    def test_accepts_hashed_requirements_with_continuations(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "requirements.txt",
            f"# comment\n--require-hashes\nfoo==1.0 \\\n    --hash={DIGEST} \\\n"
            f"    --hash={DIGEST}\n",
        )
        assert check_pins.check_requirements(tmp_path) == []

    def test_rejects_requirement_without_hash(self, tmp_path: Path) -> None:
        _write(tmp_path, "requirements-dev.txt", "foo==1.0\n")
        findings = check_pins.check_requirements(tmp_path)
        assert len(findings) == 1
        assert "foo==1.0" in findings[0]


class TestImages:
    def test_accepts_digest_scratch_and_stage_names(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "images/hub/Dockerfile",
            f"FROM python:3.12-slim@{DIGEST} AS build\nFROM build AS test\n"
            f"FROM scratch\nCOPY --from=build /a /a\nCOPY --from=0 /a /b\n",
        )
        assert check_pins.check_images(tmp_path) == []

    @pytest.mark.parametrize(
        "text",
        [
            "FROM python:3.12-slim\n",
            "FROM python@sha256:short\n",
            "ARG BASE=python\nFROM ${BASE}\n",
            f"FROM python@{DIGEST}\nCOPY --from=nginx:latest /a /a\n",
        ],
    )
    def test_rejects_unpinned_dockerfiles(self, tmp_path: Path, text: str) -> None:
        _write(tmp_path, "Dockerfile", text)
        assert check_pins.check_images(tmp_path) != []

    def test_a_compose_image_may_come_from_a_required_variable_only(self, tmp_path: Path) -> None:
        for allowed in ("${ZEPHYROS_IMAGE:?}", "sha256:" + "a" * 64):
            _write(tmp_path, "compose.yaml", f"services:\n  a:\n    image: {allowed}\n")
            assert check_pins.check_images(tmp_path) == [], allowed
        for refused in ("${IMAGE}", "${IMAGE:-nginx:latest}", "${IMAGE:?}:latest", "$IMAGE"):
            _write(tmp_path, "compose.yaml", f"services:\n  a:\n    image: {refused}\n")
            assert len(check_pins.check_images(tmp_path)) == 1, refused

    def test_checks_yaml_images(self, tmp_path: Path) -> None:
        _write(tmp_path, "compose.yaml", "services:\n  a:\n    image: nginx:latest\n")
        assert check_pins.check_images(tmp_path) != []
        _write(tmp_path, "compose.yaml", f"services:\n  a:\n    image: nginx@{DIGEST}\n")
        assert check_pins.check_images(tmp_path) == []


class TestActions:
    def test_accepts_commit_hash_and_local_actions(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            ".github/workflows/ci.yml",
            f"steps:\n  - uses: actions/checkout@{SHA} # v7\n  - uses: ./local\n"
            f"  - uses: docker://alpine@{DIGEST}\n",
        )
        assert check_pins.check_actions(tmp_path) == []

    @pytest.mark.parametrize(
        "uses",
        ["actions/checkout@v7", "actions/checkout@main", "actions/checkout", "docker://alpine:3"],
    )
    def test_rejects_unpinned_actions(self, tmp_path: Path, uses: str) -> None:
        _write(tmp_path, ".github/workflows/ci.yml", f"steps:\n  - uses: {uses}\n")
        assert check_pins.check_actions(tmp_path) != []

    def test_no_github_directory_is_fine(self, tmp_path: Path) -> None:
        assert check_pins.check_actions(tmp_path) == []


class TestInstalls:
    def test_rejects_pip_install_without_hashes(self, tmp_path: Path) -> None:
        _write(tmp_path, "scripts/setup.sh", "pip install requests\n")
        assert check_pins.check_installs(tmp_path) != []

    def test_accepts_hashed_install_and_comments(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            ".github/workflows/ci.yml",
            "steps:\n  - run: pip install --require-hashes -r r.txt\n  # pip install nothing\n",
        )
        assert check_pins.check_installs(tmp_path) == []


class TestMain:
    def test_returns_one_on_findings_and_zero_when_clean(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write(tmp_path, "uv.lock", LOCK_OK)
        _write(tmp_path, "pyproject.toml", '[build-system]\nrequires = ["uv_build==0.12.24"]\n')
        assert check_pins.main([str(tmp_path)]) == 0
        _write(tmp_path, "Dockerfile", "FROM python:3\n")
        assert check_pins.main([str(tmp_path)]) == 1
        assert "unpinned" in capsys.readouterr().out
