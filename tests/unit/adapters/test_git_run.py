"""Running git on data that may come from an agent (ATK-17): the guards of every call, and the
proof that a hook of a repository never runs for the hub.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from zephyros.adapters.git.run import TIMEOUT_SECONDS, GitError
from zephyros.adapters.git.run import git as safe_git


def test_every_call_carries_the_guards(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake(command: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
        captured["command"] = command
        captured["env"] = options.get("env")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake)
    safe_git("status", cwd=tmp_path)
    command = captured["command"]
    assert isinstance(command, list)
    for guard in (
        "core.hooksPath=/dev/null",
        "core.symlinks=false",
        "core.fsmonitor=false",
        "protocol.allow=never",
        "protocol.file.allow=always",
        "transfer.fsckObjects=true",
        "fetch.fsckObjects=true",
        "receive.fsckObjects=true",
    ):
        assert "-c" in command
        assert guard in command, guard
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_ASKPASS"] == "/bin/false"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_PAGER"] == "cat"
    assert "GIT_DIR" not in environment


def commit(where: Path, message: str, *, raw: bool = False) -> None:
    if raw:
        subprocess.run(  # reason: a fixed program, arguments as a list
            ["git", "add", "--all"],  # noqa: S607  # reason: the git of the test host
            cwd=where,
            check=True,
        )
        subprocess.run(  # noqa: S603  # reason: a fixed program, arguments as a list
            [  # noqa: S607  # reason: the git of the test host
                "git",
                "-c",
                "user.name=Someone",
                "-c",
                "user.email=someone@example.test",
                "commit",
                "--quiet",
                "-m",
                message,
            ],
            cwd=where,
            check=True,
        )
        return
    safe_git("add", "--all", cwd=where)
    safe_git("commit", "--quiet", "-m", message, cwd=where)


def test_a_hook_of_a_repository_never_runs_through_the_runner(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    safe_git("init", "--quiet", cwd=repo)
    (repo / "a.txt").write_text("a\n")
    commit(repo, "first")
    marker = tmp_path / "hook-ran"
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_text(f"#!/bin/sh\necho ran > {marker}\n")
    hook.chmod(0o755)
    # the hook is live: a plain git commit runs it (the control)
    (repo / "b.txt").write_text("b\n")
    commit(repo, "second", raw=True)
    assert marker.exists()
    marker.unlink()
    # the runner's commit: the same hook stays silent
    (repo / "c.txt").write_text("c\n")
    commit(repo, "third")
    assert not marker.exists()
    # and the hook file itself has not been changed or executed since
    assert hook.read_text().startswith("#!/bin/sh")


def test_a_git_that_hangs_is_stopped_and_named_without_a_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    given: list[object] = []

    def hanging(command: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
        given.append(options.get("timeout"))
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(subprocess, "run", hanging)
    with pytest.raises(GitError, match=r"^git fetch took too long$") as caught:
        safe_git("fetch", str(tmp_path / "remote"), cwd=tmp_path)
    assert str(tmp_path) not in str(caught.value)
    with pytest.raises(GitError):
        safe_git("push", cwd=tmp_path, timeout=5)
    assert given == [TIMEOUT_SECONDS, 5]


def test_a_failure_names_no_path_of_the_host(tmp_path: Path) -> None:
    repo = tmp_path / "elsewhere"
    repo.mkdir()
    with pytest.raises(GitError) as caught:
        safe_git("rev-parse", "--verify", "HEAD", cwd=repo)
    assert str(tmp_path) not in str(caught.value)
