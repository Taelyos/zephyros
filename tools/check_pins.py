"""Reject any dependency, container image or CI action that is not pinned by hash.

Covers CTR-10 and US-E11-05. Each check returns a list of findings, one string per
problem, shaped ``path:line: message``. An empty list means the check passed.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

SKIPPED_DIRS = {".git", ".venv", "node_modules", ".mypy_cache", ".ruff_cache", ".pytest_cache"}

_SHA256_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"(?:@|^)sha256:[0-9a-f]{64}$")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_FROM = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(?P<image>\S+)(?:\s+AS\s+(?P<alias>\S+))?\s*$", re.I)
_COPY_FROM = re.compile(r"^\s*(?:COPY|ADD)\s+.*?--from=(?P<source>\S+)", re.I)
_YAML_IMAGE = re.compile(r"^\s*(?:-\s+)?image:\s*['\"]?(?P<image>[^'\"\s#]+)")
_REQUIRED_VARIABLE = re.compile(r"^\$\{[A-Z][A-Z0-9_]*:\?\}$")
"""A compose file may take its image from a required variable with no default, `${NAME:?}`:
the program that sets it pins it (scripts/deploy.sh sets an image identifier, sha256:...)."""
_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*['\"]?(?P<ref>[^'\"\s#]+)")
_PIP_INSTALL = re.compile(r"\b(?:uv\s+)?pip\s+install\b")


def _files(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    found: set[Path] = set()
    for pattern in patterns:
        for path in root.rglob(pattern):
            relative = path.relative_to(root)
            if path.is_file() and not SKIPPED_DIRS.intersection(relative.parts):
                found.add(path)
    return sorted(found)


def check_lock(lock_path: Path) -> list[str]:
    """Every locked Python package comes from the registry with a sha256 per artifact."""
    if not lock_path.is_file():
        return [f"{lock_path}:1: lock file is missing"]
    data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    findings: list[str] = []
    for package in data.get("package", []):
        name = package.get("name", "?")
        source = package.get("source", {})
        if "editable" in source or "virtual" in source:
            continue  # the project itself
        if "registry" not in source:
            findings.append(f"{lock_path}:1: package {name} has a source that is not a registry")
            continue
        artifacts = list(package.get("wheels", []))
        if "sdist" in package:
            artifacts.append(package["sdist"])
        if not artifacts:
            findings.append(f"{lock_path}:1: package {name} has no artifact")
        findings.extend(
            f"{lock_path}:1: an artifact of package {name} has no sha256 hash"
            for artifact in artifacts
            if not _SHA256_HASH.match(str(artifact.get("hash", "")))
        )
    return findings


def check_build_system(pyproject: Path) -> list[str]:
    """Build requirements are pinned to an exact version.

    A build backend cannot carry a hash in `[build-system]`. The project uses the backend that
    uv bundles (`uv_build`), which uv runs without downloading it, so the exact version is
    what ties it to the pinned uv. See docs/development.md.
    """
    if not pyproject.is_file():
        return [f"{pyproject}:1: pyproject.toml is missing"]
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    requires = data.get("build-system", {}).get("requires", [])
    if not requires:
        return [f"{pyproject}:1: no build requirement is declared"]
    return [
        f"{pyproject}:1: build requirement not pinned to an exact version: {requirement}"
        for requirement in requires
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+==[0-9][A-Za-z0-9.+!]*", requirement.strip())
    ]


def check_requirements(root: Path) -> list[str]:
    """Every requirement of a requirements file carries a hash."""
    findings: list[str] = []
    for path in _files(root, ("requirements*.txt",)):
        logical = ""
        start = 0
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not logical:
                start = number
            line = raw.strip()
            logical += " " + line.removesuffix("\\")
            if line.endswith("\\"):
                continue
            text = logical.strip()
            logical = ""
            if not text or text.startswith(("#", "-")):
                continue
            if "--hash=sha256:" not in text:
                findings.append(f"{path}:{start}: requirement without a hash: {text.split()[0]}")
    return findings


def check_images(root: Path) -> list[str]:
    """Every base image of a Dockerfile and every image in a YAML file has a digest."""
    findings: list[str] = []
    for path in _files(root, ("Dockerfile", "Dockerfile.*", "*.Dockerfile", "*.dockerfile")):
        stages: set[str] = set()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = _FROM.match(line)
            if match:
                image = match["image"]
                if image.lower() != "scratch" and image.lower() not in stages:
                    if "$" in image:
                        findings.append(f"{path}:{number}: image reference built from a variable")
                    elif not _IMAGE_DIGEST.search(image):
                        findings.append(f"{path}:{number}: image without a digest: {image}")
                if match["alias"]:
                    stages.add(match["alias"].lower())
                continue
            copy = _COPY_FROM.match(line)
            if copy:
                source = copy["source"]
                is_stage = source.isdigit() or source.lower() in stages
                if not is_stage and not _IMAGE_DIGEST.search(source):
                    findings.append(f"{path}:{number}: copy from an image without a digest")
    for path in _files(root, ("*.yml", "*.yaml")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = _YAML_IMAGE.match(line)
            image = match["image"] if match else ""
            if match and not _IMAGE_DIGEST.search(image) and not _REQUIRED_VARIABLE.match(image):
                findings.append(f"{path}:{number}: image without a digest: {match['image']}")
    return findings


def check_actions(root: Path) -> list[str]:
    """Every action of a CI workflow is referenced by a commit hash."""
    findings: list[str] = []
    workflows = root / ".github"
    if not workflows.is_dir():
        return findings
    for path in _files(workflows, ("*.yml", "*.yaml")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = _USES.match(line)
            if not match:
                continue
            reference = match["ref"]
            if reference.startswith("./"):
                continue
            if reference.startswith("docker://"):
                if not _IMAGE_DIGEST.search(reference):
                    findings.append(f"{path}:{number}: docker action without a digest: {reference}")
                continue
            _, _, version = reference.partition("@")
            if not _COMMIT_SHA.match(version):
                findings.append(f"{path}:{number}: action not pinned by commit hash: {reference}")
    return findings


def check_installs(root: Path) -> list[str]:
    """A pip install in a workflow or a script must require hashes."""
    findings: list[str] = []
    paths = _files(root / ".github", ("*.yml", "*.yaml")) if (root / ".github").is_dir() else []
    paths += _files(root / "scripts", ("*.sh",)) if (root / "scripts").is_dir() else []
    for path in paths:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if _PIP_INSTALL.search(line) and "--require-hashes" not in line:
                findings.append(f"{path}:{number}: pip install without --require-hashes")
    return findings


def run_all(root: Path) -> list[str]:
    return [
        *check_lock(root / "uv.lock"),
        *check_build_system(root / "pyproject.toml"),
        *check_requirements(root),
        *check_images(root),
        *check_actions(root),
        *check_installs(root),
    ]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]) if args else Path.cwd()
    findings = run_all(root)
    for finding in findings:
        print(finding)
    if findings:
        print(f"{len(findings)} unpinned reference(s).")
        return 1
    print("All dependencies, images and actions are pinned by hash.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
