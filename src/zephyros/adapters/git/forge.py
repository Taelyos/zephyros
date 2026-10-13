"""A forge in a directory: bare repositories and pull requests in JSON files.

It stands in for GitHub in the delivery loop of the hub (the "dummy forge" of the walking
skeleton): `<root>/<owner>/<name>.git` is a repository, `<root>/<owner>/<name>.pulls.json` its
pull requests. A package is a git bundle. Nothing here reaches a network.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path

from zephyros.adapters.git.run import GitError, bundle, git, heads_of_bundle, write_package
from zephyros.core.ports.forge import (
    INTERFACE_VERSION,
    ForgeError,
    PullRequest,
    PullRequestRequest,
    RepositoryRef,
)

_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")


class DirectoryForge:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._lock = asyncio.Lock()

    @property
    def interface_version(self) -> str:
        return INTERFACE_VERSION

    # ------------------------------------------------------------------ pull requests
    async def open_pull_request(self, request: PullRequestRequest) -> PullRequest:
        repository = self._repository(request.repository)
        if not request.title.strip():
            raise ForgeError("the title is empty")
        for branch in (request.head, request.base):
            if not self._has_branch(repository, branch):
                raise ForgeError(f"unknown branch in {request.repository.slug}: {branch}")
        async with self._lock:
            pulls = self._pulls(request.repository)
            number = len(pulls) + 1
            opened = PullRequest(
                request.repository,
                number,
                f"https://forge.invalid/{request.repository.slug}/pull/{number}",
                request.head,
                request.base,
                request.title,
                request.body,
            )
            pulls.append(
                {
                    "number": number,
                    "url": opened.url,
                    "head": opened.head,
                    "base": opened.base,
                    "title": opened.title,
                    "body": opened.body,
                }
            )
            self._save_pulls(request.repository, pulls)
            return opened

    async def get_pull_request(self, repository: RepositoryRef, number: int) -> PullRequest:
        self._repository(repository)
        for item in self._pulls(repository):
            if item["number"] == number:
                return PullRequest(
                    repository,
                    number,
                    str(item["url"]),
                    str(item["head"]),
                    str(item["base"]),
                    str(item["title"]),
                    str(item["body"]),
                )
        raise ForgeError(f"no pull request {number} in {repository.slug}")

    # ------------------------------------------------------------------ branches
    async def fetch(self, repository: RepositoryRef, branch: str) -> bytes:
        path = self._repository(repository)
        if not self._has_branch(path, branch):
            raise ForgeError(f"unknown branch in {repository.slug}: {branch}")
        try:
            return bundle(path, [f"refs/heads/{branch}"])
        except GitError as error:
            raise ForgeError(str(error)) from None

    async def push(self, repository: RepositoryRef, branch: str, package: bytes) -> None:
        path = self._repository(repository)
        if not _BRANCH.fullmatch(branch) or ".." in branch:
            raise ForgeError("the branch name is not plain")
        if self._has_branch(path, branch):
            raise ForgeError(f"the branch exists: {branch}")
        async with self._lock:
            with tempfile.TemporaryDirectory(dir=path) as scratch:
                incoming = write_package(Path(scratch), package)
                try:
                    heads = heads_of_bundle(path, incoming)
                    if len(set(heads.values())) != 1:
                        raise ForgeError("the package must carry one state")
                    source = min(heads, key=lambda ref: (not ref.startswith("refs/heads/"), ref))
                    git(
                        "fetch",
                        "--quiet",
                        "--no-tags",
                        str(incoming),
                        f"{source}:refs/heads/{branch}",
                        cwd=path,
                    )
                except GitError as error:
                    raise ForgeError(str(error)) from None

    async def aclose(self) -> None:
        return None

    # ------------------------------------------------------------------ helpers
    def _repository(self, repository: RepositoryRef) -> Path:
        owner, _, name = repository.slug.partition("/")
        if not _PART.fullmatch(owner) or not _PART.fullmatch(name):
            raise ForgeError(f"unknown repository: {repository.slug}")
        path = self._root / owner / f"{name}.git"
        if not (path / "HEAD").exists():
            raise ForgeError(f"unknown repository: {repository.slug}")
        return path

    @staticmethod
    def _has_branch(path: Path, branch: str) -> bool:
        if not _BRANCH.fullmatch(branch) or ".." in branch:
            return False
        found = git(
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}^{{commit}}",
            cwd=path,
            check=False,
        )
        return found.returncode == 0

    def _pulls_path(self, repository: RepositoryRef) -> Path:
        owner, _, name = repository.slug.partition("/")
        return self._root / owner / f"{name}.pulls.json"

    def _pulls(self, repository: RepositoryRef) -> list[dict[str, object]]:
        path = self._pulls_path(repository)
        if not path.exists():
            return []
        data: list[dict[str, object]] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def _save_pulls(self, repository: RepositoryRef, pulls: list[dict[str, object]]) -> None:
        path = self._pulls_path(repository)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(pulls, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
