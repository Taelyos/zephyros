"""The GitHub forge: what the conformance suite does not reach — the token never travels, the
API calls carry what GitHub expects, and a failure says nothing a token could hide in."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.support.git_fixtures import make_bundle, make_forge_repository
from zephyros.adapters.git.run import git
from zephyros.adapters.github.forge import GitHubForge
from zephyros.core.ports.forge import ForgeError, PullRequestRequest, RepositoryRef

TOKEN = "gho_unit0000000000000000000000000000000000"  # pragma: allowlist secret


def make(
    tmp_path: Path,
    handler: Any = None,
    *,
    git_base: str = "https://github.com",
) -> GitHubForge:
    token = tmp_path / "token.key"
    token.write_text(TOKEN + "\n", encoding="utf-8")
    fallback = lambda request: httpx.Response(  # noqa: E731  # reason: a test double
        404, json={"message": "Not Found"}
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler or fallback))
    return GitHubForge(token, git_base=git_base, api_base="https://api.github.test", client=client)


def test_an_empty_title_is_refused_without_a_call(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(201, json={})

    forge = make(tmp_path, handler)
    request = PullRequestRequest(RepositoryRef("org/project"), "head", "main", "  ", "body")
    with pytest.raises(ForgeError, match="the title is empty"):
        asyncio.run(forge.open_pull_request(request))
    assert calls == []


def test_a_repository_name_the_forge_cannot_hold_is_refused_without_a_call(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(201, json={})

    forge = make(tmp_path, handler)
    request = PullRequestRequest(RepositoryRef("org/-bad"), "head", "main", "t", "b")
    with pytest.raises(ForgeError, match="unknown repository"):
        asyncio.run(forge.open_pull_request(request))
    assert calls == []


def test_a_repository_github_does_not_know_is_refused_by_the_api(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    forge = make(tmp_path, handler)
    request = PullRequestRequest(RepositoryRef("nobody/nothing"), "head", "main", "t", "b")
    with pytest.raises(ForgeError, match="HTTP 404"):
        asyncio.run(forge.open_pull_request(request))


def test_a_pull_request_is_opened_with_the_headers_github_expects(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["agent"] = request.headers.get("user-agent")
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "number": 7,
                "html_url": "https://github.com/org/project/pull/7",
                "head": {"ref": "zephyros/d1-work"},
                "base": {"ref": "main"},
                "title": "A title",
                "body": "A body",
            },
        )

    forge = make(tmp_path, handler)
    request = PullRequestRequest(
        RepositoryRef("org/project"), "zephyros/d1-work", "main", "A title", "A body"
    )
    opened = asyncio.run(forge.open_pull_request(request))
    assert (opened.number, opened.url, opened.head, opened.base) == (
        7,
        "https://github.com/org/project/pull/7",
        "zephyros/d1-work",
        "main",
    )
    assert (opened.title, opened.body) == ("A title", "A body")
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert seen["agent"]
    assert seen["path"] == "/repos/org/project/pulls"
    assert seen["body"]["head"] == "zephyros/d1-work"


def test_a_failure_never_carries_the_token(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": f"boom while reading {TOKEN} here"})

    forge = make(tmp_path, handler)
    request = PullRequestRequest(RepositoryRef("org/project"), "head", "main", "t", "b")
    with pytest.raises(ForgeError) as caught:
        asyncio.run(forge.open_pull_request(request))
    message = str(caught.value)
    assert TOKEN not in message
    assert "[redacted]" in message


def test_an_https_failure_carries_no_token_and_no_prompt(tmp_path: Path) -> None:
    """The git side, over https: the token lives in the credential helper's file, never in the
    command line or the environment, so a failure cannot quote it."""
    forge = make(tmp_path, git_base="https://127.0.0.1:9")
    with pytest.raises(ForgeError) as caught:
        asyncio.run(forge.fetch(RepositoryRef("org/project"), "main"))
    message = str(caught.value)
    assert TOKEN not in message
    assert "unknown repository or branch" in message


def test_the_auth_configuration_never_holds_the_token(tmp_path: Path) -> None:
    forge = make(tmp_path)
    config = forge._auth_config()
    joined = " ".join(config)
    assert TOKEN not in joined
    assert "protocol.https.allow=always" in config
    assert "credential.helper=" in joined
    assert str(tmp_path / "token.key") in joined


def serve(handler: Any) -> tuple[HTTPServer, str]:
    """A real local HTTP server: the non-injected path makes its own client per call."""

    class Wrapper(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # reason: the protocol's names
            self.answer()

        def do_POST(self) -> None:  # reason: the protocol's names
            self.answer()

        def answer(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            status, payload = handler(self.command, self.path, body)
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Wrapper)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_two_calls_in_two_event_loops_work(tmp_path: Path) -> None:
    """Every hub call is its own `asyncio.run`: a client or a lock kept between calls would
    outlive its loop — the steam-watch run's RuntimeError."""
    pulls: list[dict[str, object]] = []

    def handler(method: str, path: str, body: bytes) -> tuple[int, object]:
        if method == "POST" and path.endswith("/pulls"):
            number = len(pulls) + 1
            pull: dict[str, object] = {
                "number": number,
                "html_url": f"https://github.com/org/project/pull/{number}",
                "head": {"ref": "head"},
                "base": {"ref": "main"},
                "title": "t",
                "body": "b",
            }
            pulls.append(pull)
            return 201, pull
        return 404, {"message": "Not Found"}

    server, base = serve(handler)
    try:
        token = tmp_path / "token.key"
        token.write_text(TOKEN + "\n", encoding="utf-8")
        forge = GitHubForge(token, api_base=base)
        request = PullRequestRequest(RepositoryRef("org/project"), "head", "main", "t", "b")
        first = asyncio.run(forge.open_pull_request(request))
        second = asyncio.run(forge.open_pull_request(request))
        assert (first.number, second.number) == (1, 2)
    finally:
        server.shutdown()


def test_two_pushes_in_two_event_loops_work(tmp_path: Path) -> None:
    root = tmp_path / "forge"
    root.mkdir()
    bare = root / "org" / "project.git"
    make_forge_repository(root, "org/project", {"main": {"a.txt": b"a\n"}})

    def handler(method: str, path: str, body: bytes) -> tuple[int, object]:
        parts = path.strip("/").split("/")
        if parts[3:5] == ["git", "ref"] and len(parts) == 7 and parts[5] == "heads":
            branch = parts[6]
            found = git(
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}^{{commit}}",
                cwd=bare,
                check=False,
            )
            if found.returncode == 0:
                return 200, {"ref": f"refs/heads/{branch}"}
            return 404, {"message": "Not Found"}
        return 404, {"message": "Not Found"}

    server, base = serve(handler)
    try:
        token = tmp_path / "token.key"
        token.write_text(TOKEN + "\n", encoding="utf-8")
        forge = GitHubForge(token, git_base=str(root), api_base=base)
        package = make_bundle({"a.txt": b"c\n"}, branch="anything")
        asyncio.run(forge.push(RepositoryRef("org/project"), "one", package))
        asyncio.run(forge.push(RepositoryRef("org/project"), "two", package))
        for branch in ("one", "two"):
            git("rev-parse", "--verify", f"refs/heads/{branch}", cwd=bare)
    finally:
        server.shutdown()


def test_a_comment_is_posted_where_github_keeps_the_comments_of_a_pull_request(
    tmp_path: Path,
) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"], seen["path"] = request.method, request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 1})

    forge = make(tmp_path, handler)
    asyncio.run(forge.comment(RepositoryRef("org/project"), 7, "The verdict."))
    assert (seen["method"], seen["path"]) == ("POST", "/repos/org/project/issues/7/comments")
    assert seen["body"] == {"body": "The verdict."}
    with pytest.raises(ForgeError):
        asyncio.run(make(tmp_path).comment(RepositoryRef("org/project"), 8, "x"))
    with pytest.raises(ForgeError):
        asyncio.run(forge.comment(RepositoryRef("org/project"), 7, " "))


def test_a_retitle_patches_the_pull_request_with_the_new_title(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        seen["method"], seen["path"] = request.method, request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"number": 7, "title": "A new title"})

    forge = make(tmp_path, handler)
    asyncio.run(forge.update_title(RepositoryRef("org/project"), 7, "A new title"))
    assert (seen["method"], seen["path"]) == ("PATCH", "/repos/org/project/pulls/7")
    assert seen["body"] == {"title": "A new title"}
    before = list(calls)
    with pytest.raises(ForgeError, match="the title is empty"):
        asyncio.run(forge.update_title(RepositoryRef("org/project"), 7, "  "))
    assert calls == before
