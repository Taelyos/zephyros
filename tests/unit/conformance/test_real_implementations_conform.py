"""CTR-11: the real implementations pass the conformance suite of their interface.

The Hermes engine only describes turns, so it is checked as it is.

The OpenAI-compatible provider is checked against a small fake of the service it forwards to,
reached through an in-process transport: no network, no credential of a real provider.
"""

from __future__ import annotations

import atexit
import json
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

import httpx

from tests.support.git_fixtures import make_bundle, make_forge_repository
from zephyros.adapters.git.delivery import GitDeliveryArea
from zephyros.adapters.git.forge import DirectoryForge
from zephyros.adapters.git.run import git
from zephyros.adapters.github.forge import GitHubForge
from zephyros.adapters.hermes.engine import HermesEngine
from zephyros.adapters.providers.openai_compatible import FileCredential, OpenAiCompatibleProvider
from zephyros.conformance import (
    AgentEngineConformance,
    DeliveryConformance,
    DeliveryFixture,
    ForgeConformance,
    ForgeFixture,
    ModelProviderConformance,
)
from zephyros.core.ports.agent_engine import AgentEngine
from zephyros.core.ports.forge import RepositoryRef
from zephyros.core.ports.model_provider import ModelProvider


def fake_upstream(request: httpx.Request) -> httpx.Response:
    """An OpenAI-compatible service that calls the first offered tool when there is one."""
    body = json.loads(request.content)
    if request.headers.get("authorization") != "Bearer sk-conformance-fake":
        return httpx.Response(401, json={"error": "bad key"})
    message: dict[str, object] = {"role": "assistant", "content": "ok"}
    if body.get("tools"):
        name = body["tools"][0]["function"]["name"]
        message["tool_calls"] = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": name, "arguments": '{"text": "hi"}'},
            }
        ]
    return httpx.Response(
        200,
        json={
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
        },
    )


class TestOpenAiCompatibleProvider(ModelProviderConformance):
    implementation: ClassVar[type] = OpenAiCompatibleProvider

    async def make_provider(self) -> ModelProvider:
        directory = Path(tempfile.mkdtemp(prefix="zephyros-conformance-"))
        atexit.register(shutil.rmtree, directory, ignore_errors=True)
        credential = directory / "model.key"
        credential.write_text("sk-conformance-fake\n", encoding="utf-8")
        client = httpx.AsyncClient(transport=httpx.MockTransport(fake_upstream))
        return OpenAiCompatibleProvider(
            "https://models.invalid/v1",
            FileCredential(credential),
            upstream_model="upstream-model",
            client=client,
        )


class TestHermesEngine(AgentEngineConformance):
    implementation: ClassVar[type] = HermesEngine

    def make_engine(self) -> AgentEngine:
        return HermesEngine()


class TestGitDeliveryArea(DeliveryConformance):
    implementation: ClassVar[type] = GitDeliveryArea

    def make_fixture(self) -> DeliveryFixture:
        root = Path(tempfile.mkdtemp(prefix="zd"))
        atexit.register(shutil.rmtree, root, ignore_errors=True)
        return DeliveryFixture(GitDeliveryArea(root), make_bundle)


class TestDirectoryForge(ForgeConformance):
    implementation: ClassVar[type] = DirectoryForge

    async def make_fixture(self) -> ForgeFixture:
        root = Path(tempfile.mkdtemp(prefix="zf"))
        atexit.register(shutil.rmtree, root, ignore_errors=True)
        make_forge_repository(
            root, "org/project", {"main": {"a.txt": b"a\n"}, "feature": {"a.txt": b"b\n"}}
        )
        package = make_bundle({"a.txt": b"c\n"}, branch="anything")
        return ForgeFixture(
            DirectoryForge(root), RepositoryRef("org/project"), "feature", "main", package
        )


# ----------------------------------------------------------------- GitHub, against a fake API


def _bare_of(root: Path, slug: str) -> Path:
    owner, _, name = slug.partition("/")
    return root / owner / f"{name}.git"


def _branch_exists(root: Path, slug: str, branch: str) -> bool:
    found = git(
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/heads/{branch}^{{commit}}",
        cwd=_bare_of(root, slug),
        check=False,
    )
    return found.returncode == 0


def fake_github(
    root: Path, state: dict[str, list[dict[str, object]]]
) -> Callable[[httpx.Request], httpx.Response]:
    """A GitHub API over a directory of bare repositories: the checks of the forge, offline."""

    def handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        if len(parts) < 3 or parts[0] != "repos":
            return httpx.Response(404, json={"message": "Not Found"})
        slug = f"{parts[1]}/{parts[2]}"
        if not (_bare_of(root, slug) / "HEAD").exists():
            return httpx.Response(404, json={"message": "Not Found"})
        rest = parts[3:]
        if rest[:2] == ["git", "ref"] and len(rest) == 4 and rest[2] == "heads":
            if _branch_exists(root, slug, rest[3]):
                return httpx.Response(200, json={"ref": f"refs/heads/{rest[3]}"})
            return httpx.Response(404, json={"message": "Not Found"})
        if rest == ["pulls"] and request.method == "POST":
            body = json.loads(request.content)
            head, base = str(body["head"]), str(body["base"])
            for branch in (head, base):
                if not _branch_exists(root, slug, branch):
                    return httpx.Response(422, json={"message": "Validation Failed"})
            number = len(state.setdefault(slug, [])) + 1
            pull: dict[str, object] = {
                "number": number,
                "html_url": f"https://github.com/{slug}/pull/{number}",
                "head": {"ref": head},
                "base": {"ref": base},
                "title": body["title"],
                "body": body.get("body", ""),
            }
            state[slug].append(pull)
            return httpx.Response(201, json=pull)
        if len(rest) == 2 and rest[0] == "pulls" and request.method == "GET":
            number = int(rest[1])
            for pull in state.get(slug, []):
                if pull["number"] == number:
                    return httpx.Response(200, json=pull)
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404, json={"message": "Not Found"})

    return handler


class TestGitHubForge(ForgeConformance):
    implementation: ClassVar[type] = GitHubForge

    async def make_fixture(self) -> ForgeFixture:
        root = Path(tempfile.mkdtemp(prefix="zgh"))
        atexit.register(shutil.rmtree, root, ignore_errors=True)
        make_forge_repository(
            root, "org/project", {"main": {"a.txt": b"a\n"}, "feature": {"a.txt": b"b\n"}}
        )
        token = root / "token.key"
        token.write_text(
            "gho_conformance00000000000000000000000000\n",  # pragma: allowlist secret
            encoding="utf-8",
        )
        package = make_bundle({"a.txt": b"c\n"}, branch="anything")
        client = httpx.AsyncClient(transport=httpx.MockTransport(fake_github(root, {})))
        forge = GitHubForge(
            token, git_base=str(root), api_base="https://api.github.test", client=client
        )
        return ForgeFixture(forge, RepositoryRef("org/project"), "feature", "main", package)
