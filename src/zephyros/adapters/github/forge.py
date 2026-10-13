"""A forge on GitHub: repositories, branches and pull requests over its API and git.

The hub opens pull requests on GitHub (annex "Accepted repository sources", version 1: the https
URLs of GitHub repositories). The token is read from a file at each call; it never appears in a
command line, a message, an error or a log record — git receives it through a credential helper
that reads the file itself, and every message built here is redacted against it.

A package is a git bundle, the format of the delivery area.
"""

from __future__ import annotations

import re
import shlex
import tempfile
from pathlib import Path

import httpx

from zephyros.adapters.git.run import (
    GitError,
    bundle,
    git,
    heads_of_bundle,
    init_bare,
    write_package,
)
from zephyros.core.ports.forge import (
    INTERFACE_VERSION,
    ForgeError,
    PullRequest,
    PullRequestRequest,
    RepositoryRef,
)

TIMEOUT_SECONDS = 120.0
_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_USER_AGENT = "zephyros-hub"
_API_VERSION = "2022-11-28"


class GitHubForge:
    """GitHub as the forge of the hub. One token file; no other credential."""

    def __init__(
        self,
        token_file: Path,
        *,
        git_base: str = "https://github.com",
        api_base: str = "https://api.github.com",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token_file = token_file
        self._git_base = git_base.rstrip("/")
        self._api_base = api_base.rstrip("/")
        self._client = client

    @property
    def interface_version(self) -> str:
        return INTERFACE_VERSION

    # ------------------------------------------------------------------ the token
    def _token(self) -> str:
        try:
            return self._token_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _redact(self, text: str) -> str:
        token = self._token()
        if token:
            text = text.replace(token, "[redacted]")
        return text

    def _auth_config(self) -> list[str]:
        """What git needs for an https forge: the protocol, and a helper that reads the token
        file itself — so the token is in no command line and in no environment."""
        if not self._git_base.startswith("https://"):
            return []
        helper = (
            "!f() { printf 'username=x-access-token\\npassword=%s\\n' "
            f'"$(cat {shlex.quote(str(self._token_file))})"; }}; f'
        )
        return ["protocol.https.allow=always", f"credential.helper={helper}"]

    def _git_url(self, repository: RepositoryRef) -> str:
        return f"{self._git_base}/{repository.slug}.git"

    @staticmethod
    def _validate(repository: RepositoryRef) -> None:
        owner, _, name = repository.slug.partition("/")
        if not _PART.fullmatch(owner) or not _PART.fullmatch(name):
            raise ForgeError(f"unknown repository: {repository.slug}")

    @staticmethod
    def _plain_branch(branch: str) -> bool:
        return bool(_BRANCH.fullmatch(branch)) and ".." not in branch

    # ------------------------------------------------------------------ the API
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION,
            "User-Agent": _USER_AGENT,
        }

    async def _call(
        self, method: str, path: str, body: dict[str, object] | None = None
    ) -> httpx.Response:
        # The delivery worker runs one job at a time, and each call is its own event loop
        # (`asyncio.run`): a client kept between calls would outlive its loop. When no client
        # is injected (the tests), one is made for this call alone.
        client = self._client
        owned = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False)
        try:
            return await client.request(
                method, f"{self._api_base}{path}", json=body, headers=self._headers()
            )
        except httpx.HTTPError as error:
            reason = type(error).__name__
            raise ForgeError(self._redact(f"the forge is unreachable: {reason}")) from None
        finally:
            if owned:
                await client.aclose()

    def _failure(self, response: httpx.Response, what: str) -> ForgeError:
        detail = ""
        try:
            data = response.json()
            if isinstance(data, dict):
                detail = str(data.get("message", ""))
        except ValueError:
            pass
        suffix = f" ({detail})" if detail else ""
        return ForgeError(self._redact(f"{what}: HTTP {response.status_code}{suffix}"))

    @staticmethod
    def _pull(repository: RepositoryRef, data: dict[str, object]) -> PullRequest:
        head = data["head"]
        base = data["base"]
        assert isinstance(head, dict)  # noqa: S101  # reason: the API answered it
        assert isinstance(base, dict)  # noqa: S101  # reason: the API answered it
        return PullRequest(
            repository,
            int(str(data["number"])),
            str(data["html_url"]),
            str(head["ref"]),
            str(base["ref"]),
            str(data.get("title", "")),
            str(data.get("body") or ""),
        )

    # ------------------------------------------------------------------ pull requests
    async def open_pull_request(self, request: PullRequestRequest) -> PullRequest:
        self._validate(request.repository)
        if not request.title.strip():
            raise ForgeError("the title is empty")
        if not self._plain_branch(request.head) or not self._plain_branch(request.base):
            raise ForgeError("a branch name is not plain")
        response = await self._call(
            "POST",
            f"/repos/{request.repository.slug}/pulls",
            {
                "title": request.title,
                "body": request.body,
                "head": request.head,
                "base": request.base,
            },
        )
        if response.status_code != 201:
            what = f"cannot open a pull request on {request.repository.slug}"
            raise self._failure(response, what)
        data = response.json()
        return self._pull(request.repository, data)

    async def get_pull_request(self, repository: RepositoryRef, number: int) -> PullRequest:
        self._validate(repository)
        response = await self._call("GET", f"/repos/{repository.slug}/pulls/{number}")
        if response.status_code != 200:
            raise self._failure(response, f"no pull request {number} in {repository.slug}")
        data = response.json()
        return self._pull(repository, data)

    async def comment(self, repository: RepositoryRef, number: int, body: str) -> None:
        self._validate(repository)
        if not body.strip():
            raise ForgeError("the comment is empty")
        response = await self._call(
            "POST", f"/repos/{repository.slug}/issues/{number}/comments", {"body": body}
        )
        if response.status_code != 201:
            raise self._failure(response, f"cannot comment on pull request {number}")

    async def update_title(self, repository: RepositoryRef, number: int, title: str) -> None:
        self._validate(repository)
        if not title.strip():
            raise ForgeError("the title is empty")
        response = await self._call(
            "PATCH", f"/repos/{repository.slug}/pulls/{number}", {"title": title}
        )
        if response.status_code != 200:
            raise self._failure(response, f"cannot retitle pull request {number}")

    # ------------------------------------------------------------------ branches
    async def fetch(self, repository: RepositoryRef, branch: str) -> bytes:
        self._validate(repository)
        if not self._plain_branch(branch):
            raise ForgeError(f"unknown branch in {repository.slug}: {branch}")
        with tempfile.TemporaryDirectory(prefix="zg-fetch-") as scratch:
            path = Path(scratch) / "repo"
            try:
                init_bare(path)
                git(
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    self._git_url(repository),
                    f"refs/heads/{branch}:refs/heads/{branch}",
                    cwd=path,
                    extra_config=self._auth_config(),
                )
            except GitError as error:
                raise ForgeError(
                    self._redact(
                        f"unknown repository or branch: {repository.slug} {branch} ({error})"
                    )
                ) from None
            try:
                return bundle(path, [f"refs/heads/{branch}"])
            except GitError as error:
                raise ForgeError(self._redact(str(error))) from None

    async def push(self, repository: RepositoryRef, branch: str, package: bytes) -> None:
        self._validate(repository)
        if not self._plain_branch(branch):
            raise ForgeError("the branch name is not plain")
        # The branch must not exist. The check here is for the message; the push itself
        # enforces it (an empty force-with-lease refuses a branch that exists, whether the
        # push would fast-forward or not): the hub never rewrites a branch of the user.
        response = await self._call("GET", f"/repos/{repository.slug}/git/ref/heads/{branch}")
        if response.status_code == 200:
            raise ForgeError(f"the branch exists: {branch}")
        if response.status_code not in (404, 409):
            raise self._failure(response, f"cannot read {repository.slug}")
        with tempfile.TemporaryDirectory(prefix="zg-push-") as scratch:
            path = Path(scratch) / "repo"
            try:
                init_bare(path)
                incoming = write_package(Path(scratch), package)
                heads = heads_of_bundle(path, incoming)
                if len(set(heads.values())) != 1:
                    raise ForgeError("the package must carry one state: the delivered branch")
                source = min(heads, key=lambda ref: (not ref.startswith("refs/heads/"), ref))
                git(
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    str(incoming),
                    f"{source}:refs/heads/incoming",
                    cwd=path,
                )
                git(
                    "push",
                    "--quiet",
                    self._git_url(repository),
                    f"refs/heads/incoming:refs/heads/{branch}",
                    f"--force-with-lease=refs/heads/{branch}:",
                    cwd=path,
                    extra_config=self._auth_config(),
                )
            except GitError as error:
                raise ForgeError(
                    self._redact(f"the push was refused: {branch} ({error})")
                ) from None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
