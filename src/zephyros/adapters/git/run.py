"""Running git on data that may come from an agent.

Every call gets a clean environment: no system or user configuration, no hooks, no pager, no
credential helper, no prompt, no network protocol but the local ones, and object checks on every
fetch. Arguments are a list (no shell) and refs are checked by the callers before they get here.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

TIMEOUT_SECONDS = 120
_SAFE_CONFIG = (
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "core.symlinks=false",
    "core.sharedRepository=all",
    "protocol.allow=never",
    "protocol.file.allow=always",
    "http.followRedirects=false",
    # http.followRedirects=false: a redirect is a destination the configuration did not name.
    # The hub follows none (EXG-48): a fetch that would be sent elsewhere fails instead.
    "transfer.fsckObjects=true",
    "fetch.fsckObjects=true",
    "receive.fsckObjects=true",
    "gc.auto=0",
    "advice.detachedHead=false",
    "init.defaultBranch=base",
    "user.name=Zephyros",
    "user.email=hub@zephyros.invalid",
)
# core.sharedRepository=all: everything git creates (refs, objects, directories) is readable and
# writable by every user, as the forge of a development deployment expects — the hub runs under
# its own uid, the person who seeded the forge under theirs, and the hub's own data is reached by
# nobody else.


class GitError(Exception):
    """git refused or failed. The message holds no path of the host."""


def git(
    *args: str,
    cwd: Path,
    input_bytes: bytes | None = None,
    check: bool = True,
    timeout: float = TIMEOUT_SECONDS,
    extra_config: Sequence[str] = (),
) -> subprocess.CompletedProcess[bytes]:
    command: list[str] = ["git", "-c", f"safe.directory={cwd}"]
    # The repository may belong to the person who seeded it — the directory forge of a
    # development deployment, where the hub (another uid) fetches and pushes. Exactly that
    # directory is exempted from git's ownership check; everything else (hooks, filters,
    # configuration) is still switched off below. `extra_config` is the caller's own addition
    # (a forge on GitHub allows https and names its credential helper); it never loosens the
    # rules above.
    for item in (*_SAFE_CONFIG, *extra_config):
        command += ["-c", item]
    command += list(args)
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cwd),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
    }
    try:
        done = subprocess.run(  # noqa: S603  # reason: a fixed program, arguments as a list
            command,
            cwd=cwd,
            input=input_bytes,
            capture_output=True,
            env=environment,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} took too long") from None
    if check and done.returncode != 0:
        first = done.stderr.decode("utf-8", errors="replace").strip().splitlines()[:1]
        reason = first[0].replace(str(cwd), "<repository>")[:200] if first else "no message"
        raise GitError(f"git {args[0]} failed: {reason}")
    return done


def lines(output: bytes, *, nul: bool = False) -> list[str]:
    text = output.decode("utf-8", errors="surrogateescape")
    parts = text.split("\0") if nul else text.splitlines()
    return [part for part in parts if part]


def rev(cwd: Path, ref: str) -> str:
    return (
        git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=cwd)
        .stdout.decode()
        .strip()
    )


def heads_of_bundle(cwd: Path, bundle: Path) -> dict[str, str]:
    """The refs a bundle carries, after `git bundle verify` accepted it."""
    git("bundle", "verify", "--quiet", str(bundle), cwd=cwd)
    found: dict[str, str] = {}
    for line in lines(git("bundle", "list-heads", str(bundle), cwd=cwd).stdout):
        sha, _, ref = line.partition(" ")
        found[ref] = sha
    return found


def init_bare(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if not (path / "HEAD").exists():
        git("init", "--quiet", "--bare", str(path), cwd=path)


def write_package(directory: Path, package: bytes, name: str = "incoming.bundle") -> Path:
    path = directory / name
    path.write_bytes(package)
    return path


def bundle(cwd: Path, refs: Sequence[str]) -> bytes:
    target = cwd / "outgoing.bundle"
    try:
        git("bundle", "create", "--quiet", str(target), *refs, cwd=cwd)
        return target.read_bytes()
    finally:
        target.unlink(missing_ok=True)
