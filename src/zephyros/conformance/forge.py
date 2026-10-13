"""Conformance suite of the forge interface.

To check an implementation, subclass `ForgeConformance` and implement `make_fixture`: a forge
plus a repository that exists on it, with two existing branches, and a package the forge
accepts for a new branch (its format is the implementation's). The forge is built inside the
event loop of each test and closed there.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from zephyros.conformance._support import (
    ConformanceError,
    check,
    check_interface_version,
    run_in_loop,
)
from zephyros.core.ports.forge import (
    INTERFACE_VERSION,
    Forge,
    ForgeError,
    PullRequestRequest,
    RepositoryRef,
)

# Text that must come back untouched: markup, a script link, a zero-width space, controls.
HOSTILE_BODY = (
    "<script>alert(1)</script>\n[x](javascript:alert(1))\n"
    "![beacon](https://example.invalid/b.png)\n​ zero-width\ttab\r\nlast line"
)


@dataclass(frozen=True)
class ForgeFixture:
    forge: Forge
    repository: RepositoryRef
    head: str
    base: str
    package: bytes = b""
    """A package the forge accepts for a new branch."""


class ForgeConformance:
    """Rules every forge follows."""

    async def make_fixture(self) -> ForgeFixture:
        raise NotImplementedError

    def _with_fixture(self, scenario: Callable[[ForgeFixture], Awaitable[None]]) -> None:
        async def main() -> None:
            fixture = await self.make_fixture()
            try:
                await scenario(fixture)
            finally:
                await fixture.forge.aclose()

        run_in_loop(main)

    @staticmethod
    def _request(fixture: ForgeFixture, **changes: str) -> PullRequestRequest:
        values = {"head": fixture.head, "base": fixture.base, "title": "A title", "body": "A body"}
        values.update(changes)
        return PullRequestRequest(repository=fixture.repository, **values)

    @staticmethod
    async def _must_refuse(refused: Awaitable[object], what: str) -> None:
        try:
            await refused
        except ForgeError:
            return
        raise ConformanceError(f"{what} was accepted")

    def test_follows_the_tested_major_version(self) -> None:
        async def scenario(fixture: ForgeFixture) -> None:
            check_interface_version(fixture.forge.interface_version, INTERFACE_VERSION)

        self._with_fixture(scenario)

    def test_opens_a_pull_request_and_reads_it_back(self) -> None:
        async def scenario(fixture: ForgeFixture) -> None:
            opened = await fixture.forge.open_pull_request(self._request(fixture))
            check(opened.number >= 1 and opened.url, "the pull request has no number or address")
            check(
                (opened.repository, opened.head, opened.base, opened.title)
                == (fixture.repository, fixture.head, fixture.base, "A title"),
                "the pull request does not echo the request",
            )
            read = await fixture.forge.get_pull_request(fixture.repository, opened.number)
            check(read == opened, "the pull request read back differs from the one opened")

        self._with_fixture(scenario)

    def test_keeps_the_body_verbatim(self) -> None:
        async def scenario(fixture: ForgeFixture) -> None:
            request = self._request(fixture, body=HOSTILE_BODY)
            opened = await fixture.forge.open_pull_request(request)
            check(opened.body == HOSTILE_BODY, "the body changed when the pull request opened")
            read = await fixture.forge.get_pull_request(fixture.repository, opened.number)
            check(read.body == HOSTILE_BODY, "the body changed when it was read back")

        self._with_fixture(scenario)

    def test_gives_each_pull_request_its_own_number(self) -> None:
        async def scenario(fixture: ForgeFixture) -> None:
            first = await fixture.forge.open_pull_request(self._request(fixture, title="One"))
            second = await fixture.forge.open_pull_request(self._request(fixture, title="Two"))
            check(first.number != second.number, "two pull requests share a number")
            check(first.url != second.url, "two pull requests share an address")

        self._with_fixture(scenario)

    def test_refuses_an_empty_title(self) -> None:
        async def scenario(fixture: ForgeFixture) -> None:
            for title in ("", "  "):
                await self._must_refuse(
                    fixture.forge.open_pull_request(self._request(fixture, title=title)),
                    f"the title {title!r}",
                )

        self._with_fixture(scenario)

    def test_refuses_an_unknown_repository(self) -> None:
        async def scenario(fixture: ForgeFixture) -> None:
            request = PullRequestRequest(
                repository=RepositoryRef("nobody/nothing"),
                head=fixture.head,
                base=fixture.base,
                title="A title",
                body="",
            )
            await self._must_refuse(
                fixture.forge.open_pull_request(request), "an unknown repository"
            )

        self._with_fixture(scenario)

    def test_refuses_an_unknown_branch(self) -> None:
        async def scenario(fixture: ForgeFixture) -> None:
            await self._must_refuse(
                fixture.forge.open_pull_request(self._request(fixture, head="no-such-branch")),
                "an unknown branch",
            )

        self._with_fixture(scenario)

    def test_refuses_an_unknown_pull_request(self) -> None:
        async def scenario(fixture: ForgeFixture) -> None:
            await self._must_refuse(
                fixture.forge.get_pull_request(fixture.repository, 10**6), "an unknown pull request"
            )

        self._with_fixture(scenario)

    def test_comments_on_a_pull_request_and_refuses_an_unknown_one(self) -> None:
        async def scenario(fixture: ForgeFixture) -> None:
            opened = await fixture.forge.open_pull_request(self._request(fixture))
            await fixture.forge.comment(fixture.repository, opened.number, "The verdict.")
            await self._must_refuse(
                fixture.forge.comment(fixture.repository, 10**6, "x"),
                "a comment on an unknown pull request",
            )
            await self._must_refuse(
                fixture.forge.comment(fixture.repository, opened.number, "  "), "an empty comment"
            )

        self._with_fixture(scenario)

    def test_fetches_an_existing_branch_and_refuses_an_unknown_one(self) -> None:
        async def scenario(fixture: ForgeFixture) -> None:
            await fixture.forge.fetch(fixture.repository, fixture.base)
            await self._must_refuse(
                fixture.forge.fetch(fixture.repository, "no-such-branch"), "an unknown branch"
            )
            await self._must_refuse(
                fixture.forge.fetch(RepositoryRef("nobody/nothing"), fixture.base),
                "an unknown repository",
            )

        self._with_fixture(scenario)

    def test_a_pushed_branch_can_be_fetched_and_proposed(self) -> None:
        async def scenario(fixture: ForgeFixture) -> None:
            await fixture.forge.push(fixture.repository, "zephyros-pushed", fixture.package)
            try:
                fetched = await fixture.forge.fetch(fixture.repository, "zephyros-pushed")
            except ForgeError:
                raise ConformanceError("the pushed branch cannot be fetched") from None
            check(fetched, "the pushed branch comes back empty")
            request = self._request(fixture, head="zephyros-pushed")
            opened = await fixture.forge.open_pull_request(request)
            check(opened.head == "zephyros-pushed", "the pull request is not on the pushed branch")

        self._with_fixture(scenario)

    def test_never_rewrites_an_existing_branch(self) -> None:
        async def scenario(fixture: ForgeFixture) -> None:
            await self._must_refuse(
                fixture.forge.push(fixture.repository, fixture.base, fixture.package),
                "a push over an existing branch",
            )
            await self._must_refuse(
                fixture.forge.push(RepositoryRef("nobody/nothing"), "x", fixture.package),
                "a push to an unknown repository",
            )

        self._with_fixture(scenario)
