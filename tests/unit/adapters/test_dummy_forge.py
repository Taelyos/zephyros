"""The in-memory forge keeps its own promises."""

from __future__ import annotations

import asyncio

import pytest

from zephyros.adapters.dummy import InMemoryForge
from zephyros.core.ports.forge import ForgeError, PullRequestRequest, RepositoryRef

REPO = RepositoryRef("org/one")
OTHER = RepositoryRef("org/two")


def _forge() -> InMemoryForge:
    return InMemoryForge({"org/one": frozenset({"main", "x"}), "org/two": frozenset({"main", "y"})})


def _request(repository: RepositoryRef, head: str) -> PullRequestRequest:
    return PullRequestRequest(repository, head, "main", "Title", "Body")


def test_numbers_pull_requests_per_repository() -> None:
    forge = _forge()

    async def scenario() -> list[int]:
        return [
            (await forge.open_pull_request(_request(REPO, "x"))).number,
            (await forge.open_pull_request(_request(REPO, "x"))).number,
            (await forge.open_pull_request(_request(OTHER, "y"))).number,
        ]

    assert asyncio.run(scenario()) == [1, 2, 1]


def test_the_address_is_on_a_reserved_domain() -> None:
    forge = _forge()
    opened = asyncio.run(forge.open_pull_request(_request(REPO, "x")))
    assert opened.url == "https://forge.invalid/org/one/pull/1"


@pytest.mark.parametrize("number", [0, -1, 2])
def test_refuses_a_number_that_does_not_exist(number: int) -> None:
    forge = _forge()
    asyncio.run(forge.open_pull_request(_request(REPO, "x")))
    with pytest.raises(ForgeError, match="no pull request"):
        asyncio.run(forge.get_pull_request(REPO, number))


def test_refuses_a_repository_it_does_not_know_when_reading() -> None:
    with pytest.raises(ForgeError, match="no pull request"):
        asyncio.run(_forge().get_pull_request(RepositoryRef("nobody/nothing"), 1))


def test_refuses_the_base_branch_when_it_is_missing() -> None:
    request = PullRequestRequest(REPO, "x", "gone", "Title", "")
    with pytest.raises(ForgeError, match="unknown branch"):
        asyncio.run(_forge().open_pull_request(request))


def test_remembers_that_it_was_closed() -> None:
    forge = _forge()
    asyncio.run(forge.aclose())
    assert forge.closed
