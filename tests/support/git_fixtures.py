"""Real git repositories and bundles for the tests of the git adapters and of the delivery loop."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

from zephyros.adapters.git.run import bundle, git, init_bare


def _replace_files(work: Path, files: Mapping[str, bytes]) -> None:
    for tracked in git("ls-files", "-z", cwd=work).stdout.decode().split("\0"):
        if tracked:
            (work / tracked).unlink(missing_ok=True)
    for path, content in files.items():
        target = work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def make_bundle(
    files: Mapping[str, bytes], parent: bytes | None = None, branch: str = "main"
) -> bytes:
    """A bundle whose HEAD and `branch` hold `files`, one commit after `parent` (a bundle that
    clones), or a first commit."""
    scratch = Path(tempfile.mkdtemp(prefix="zg"))
    try:
        work = scratch / "work"
        if parent is None:
            work.mkdir()
            git("init", "--quiet", "-b", branch, str(work), cwd=scratch)
        else:
            (scratch / "parent.bundle").write_bytes(parent)
            git("clone", "--quiet", str(scratch / "parent.bundle"), str(work), cwd=scratch)
            git("checkout", "--quiet", "-B", branch, cwd=work)
        _replace_files(work, files)
        git("add", "--all", cwd=work)
        git("commit", "--quiet", "--allow-empty", "-m", "a state", cwd=work)
        return bundle(work, ["HEAD", branch])
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def make_forge_repository(
    root: Path, slug: str, branches: Mapping[str, Mapping[str, bytes]]
) -> Path:
    """A bare repository `<root>/<slug>.git` with the given branches, each one commit on the
    first one."""
    owner, _, name = slug.partition("/")
    bare = root / owner / f"{name}.git"
    init_bare(bare)
    first: bytes | None = None
    for branch, files in branches.items():
        package = make_bundle(files, first, branch)
        first = first or package
        scratch = Path(tempfile.mkdtemp(prefix="zf"))
        try:
            (scratch / "b.bundle").write_bytes(package)
            git(
                "fetch",
                "--quiet",
                str(scratch / "b.bundle"),
                f"refs/heads/{branch}:refs/heads/{branch}",
                cwd=bare,
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    return bare
