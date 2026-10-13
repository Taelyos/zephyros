"""The forge interface: where the hub opens pull requests.

A forge is a service that hosts git repositories and pull requests (GitHub in version 1).
Version 0.1 opened a pull request and read it back. Version 0.2 adds what the delivery loop
needs: fetch a branch of a repository as a package, and push a branch from a package. A package
is in the format of the delivery area (a git bundle for the default one). Version 0.3 adds a
comment on a pull request: the hub posts there what the Owner side records after the gate
(ADR 0020).

Version 0.x.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

INTERFACE_VERSION = "0.3.0"


class ForgeError(Exception):
    """The forge refused the request or could not honor it."""


@dataclass(frozen=True, slots=True)
class RepositoryRef:
    """A repository, by its name on the forge, for example "organization/project"."""

    slug: str

    def __post_init__(self) -> None:
        owner, _, name = self.slug.partition("/")
        if not owner or not name or "/" in name or any(c.isspace() for c in self.slug):
            raise ValueError(f"not a repository name: {self.slug!r}")


@dataclass(frozen=True, slots=True)
class PullRequestRequest:
    repository: RepositoryRef
    head: str
    base: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class PullRequest:
    repository: RepositoryRef
    number: int
    url: str
    head: str
    base: str
    title: str
    body: str


class Forge(Protocol):
    @property
    def interface_version(self) -> str:
        """The version of this interface that the implementation follows."""
        ...

    async def open_pull_request(self, request: PullRequestRequest) -> PullRequest:
        """Open a pull request, or raise ForgeError.

        The body is text: the forge stores it and returns it unchanged, and never interprets it.
        An empty title, an unknown repository and an unknown branch are refused.
        """
        ...

    async def get_pull_request(self, repository: RepositoryRef, number: int) -> PullRequest:
        """Read a pull request back, or raise ForgeError if there is none."""
        ...

    async def comment(self, repository: RepositoryRef, number: int, body: str) -> None:
        """Add a comment to a pull request, or raise ForgeError: an unknown pull request and an
        empty body are refused. The body is text, kept verbatim."""
        ...

    async def fetch(self, repository: RepositoryRef, branch: str) -> bytes:
        """The branch as a package, or ForgeError for an unknown repository or branch."""
        ...

    async def push(self, repository: RepositoryRef, branch: str, package: bytes) -> None:
        """Create the branch from a package, or ForgeError. An existing branch is refused: the
        hub never rewrites a branch of the user."""
        ...

    async def aclose(self) -> None:
        """Release what the forge holds. Called once, in the loop."""
        ...
