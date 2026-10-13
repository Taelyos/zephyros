"""An in-memory forge: repositories, branches and pull requests that live in a dictionary.

A branch holds a package as it was pushed, opaque: the forge never reads it."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from zephyros.core.ports.forge import (
    INTERFACE_VERSION,
    ForgeError,
    PullRequest,
    PullRequestRequest,
    RepositoryRef,
)


class InMemoryForge:
    """Knows the repositories it is given, with their branches, and keeps the pull requests."""

    def __init__(
        self,
        repositories: Mapping[str, frozenset[str]],
        packages: Mapping[tuple[str, str], bytes] | None = None,
    ) -> None:
        self._branches = {slug: set(branches) for slug, branches in repositories.items()}
        self._packages: dict[tuple[str, str], bytes] = dict(packages or {})
        self._pull_requests: dict[str, list[PullRequest]] = {slug: [] for slug in self._branches}
        self.comments: dict[tuple[str, int], list[str]] = {}
        """The comments of each pull request, by repository and number, in order."""
        self.closed = False

    @property
    def interface_version(self) -> str:
        return INTERFACE_VERSION

    async def open_pull_request(self, request: PullRequestRequest) -> PullRequest:
        slug = request.repository.slug
        if slug not in self._branches:
            raise ForgeError(f"unknown repository: {slug}")
        if not request.title.strip():
            raise ForgeError("the title is empty")
        for branch in (request.head, request.base):
            if branch not in self._branches[slug]:
                raise ForgeError(f"unknown branch in {slug}: {branch}")
        number = len(self._pull_requests[slug]) + 1
        pull_request = PullRequest(
            repository=request.repository,
            number=number,
            url=f"https://forge.invalid/{slug}/pull/{number}",
            head=request.head,
            base=request.base,
            title=request.title,
            body=request.body,
        )
        self._pull_requests[slug].append(pull_request)
        return pull_request

    async def get_pull_request(self, repository: RepositoryRef, number: int) -> PullRequest:
        pull_requests = self._pull_requests.get(repository.slug, [])
        if not 1 <= number <= len(pull_requests):
            raise ForgeError(f"no pull request {number} in {repository.slug}")
        return pull_requests[number - 1]

    async def comment(self, repository: RepositoryRef, number: int, body: str) -> None:
        if not 1 <= number <= len(self._pull_requests.get(repository.slug, [])):
            raise ForgeError(f"no pull request {number} in {repository.slug}")
        if not body.strip():
            raise ForgeError("the comment is empty")
        self.comments.setdefault((repository.slug, number), []).append(body)

    async def update_title(self, repository: RepositoryRef, number: int, title: str) -> None:
        pull_requests = self._pull_requests.get(repository.slug, [])
        if not 1 <= number <= len(pull_requests):
            raise ForgeError(f"no pull request {number} in {repository.slug}")
        if not title.strip():
            raise ForgeError("the title is empty")
        pull_requests[number - 1] = replace(pull_requests[number - 1], title=title)

    async def fetch(self, repository: RepositoryRef, branch: str) -> bytes:
        if branch not in self._branches.get(repository.slug, set()):
            raise ForgeError(f"unknown branch in {repository.slug}: {branch}")
        return self._packages.get((repository.slug, branch), b"")

    async def push(self, repository: RepositoryRef, branch: str, package: bytes) -> None:
        branches = self._branches.get(repository.slug)
        if branches is None:
            raise ForgeError(f"unknown repository: {repository.slug}")
        if branch in branches:
            raise ForgeError(f"the branch exists: {branch}")
        if not branch.strip():
            raise ForgeError("the branch name is empty")
        branches.add(branch)
        self._packages[(repository.slug, branch)] = package

    async def aclose(self) -> None:
        self.closed = True
