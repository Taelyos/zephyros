"""A suite that nothing can fail proves nothing.

For each check of each suite, a deliberately broken implementation must fail that check
(and, where several checks look at the same fault, only those).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from typing import cast

import pytest

from tests.unit.conformance.helpers import all_checks, failed_checks
from tests.unit.conformance.test_dummies_conform import derive_dummy, write_assertions
from zephyros.adapters.dummy import DummyAgentEngine, InMemoryForge, ScriptedModelProvider
from zephyros.adapters.dummy.delivery import InMemoryDeliveryArea, make_package
from zephyros.adapters.dummy.verification import AssertionRunner
from zephyros.conformance import (
    AgentEngineConformance,
    DeliveryConformance,
    DeliveryFixture,
    ForgeConformance,
    ForgeFixture,
    ModelProviderConformance,
    VerificationConformance,
    VerificationFixture,
)
from zephyros.core.ports.agent_engine import AgentEngine, TurnDescription, TurnRequest
from zephyros.core.ports.delivery import Delivery, DeliveryArea, DeliveryError, Tree
from zephyros.core.ports.forge import (
    Forge,
    ForgeError,
    PullRequest,
    PullRequestRequest,
    RepositoryRef,
)
from zephyros.core.ports.model_provider import (
    ChatMessage,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    Role,
    ToolCall,
    Usage,
)
from zephyros.core.ports.verification import (
    Outcome,
    VerificationRequest,
    VerificationResult,
    VerificationRunner,
)

# ----------------------------------------------------------------------------- agent engine

Change = Callable[[TurnDescription, TurnRequest], TurnDescription]


class _EngineVariant(DummyAgentEngine):
    def __init__(
        self, change: Change, *, interface: str = "0.2.0", version: str = "dummy-0"
    ) -> None:
        self._change = change
        self._interface = interface
        self._version = version

    @property
    def interface_version(self) -> str:
        return self._interface

    @property
    def engine_version(self) -> str:
        return self._version

    def describe_turn(self, request: TurnRequest) -> TurnDescription:
        return self._change(super().describe_turn(request), request)


def _without(description: TurnDescription, key: str) -> TurnDescription:
    environment = {k: v for k, v in description.environment.items() if k != key}
    return replace(description, environment=environment)


def _with(description: TurnDescription, **extra: str) -> TurnDescription:
    return replace(description, environment={**description.environment, **extra})


def _same(description: TurnDescription, request: TurnRequest) -> TurnDescription:
    return description


def _stale_token() -> Change:
    seen: list[str] = []

    def change(description: TurnDescription, request: TurnRequest) -> TurnDescription:
        seen.append(request.token)
        return _with(description, ZEPHYROS_TOKEN=seen[0])

    return change


def _drifting() -> Change:
    calls: list[int] = []

    def change(description: TurnDescription, request: TurnRequest) -> TurnDescription:
        calls.append(1)
        return _with(description, N=str(len(calls)))

    return change


ENGINE_VARIANTS: dict[str, tuple[Callable[[], AgentEngine], set[str]]] = {
    "a later major version": (
        lambda: _EngineVariant(_same, interface="1.0.0"),
        {"test_follows_the_tested_major_version"},
    ),
    "a version that is not a number": (
        lambda: _EngineVariant(_same, interface="latest"),
        {"test_follows_the_tested_major_version"},
    ),
    "no engine version": (
        lambda: _EngineVariant(_same, version="  "),
        {"test_reports_the_engine_version"},
    ),
    "an empty command": (
        lambda: _EngineVariant(lambda d, r: replace(d, command=())),
        {"test_describes_the_turn_with_a_command", "test_keeps_the_prompt_whole_in_one_word"},
    ),
    "an empty word in the command": (
        lambda: _EngineVariant(lambda d, r: replace(d, command=(*d.command, ""))),
        {"test_describes_the_turn_with_a_command"},
    ),
    "a description that drifts": (
        lambda: _EngineVariant(_drifting()),
        {"test_gives_the_same_description_for_the_same_request"},
    ),
    "the token on the command line": (
        lambda: _EngineVariant(lambda d, r: replace(d, command=(*d.command, r.token))),
        {"test_hands_the_token_through_the_environment_only"},
    ),
    "the token in two variables": (
        lambda: _EngineVariant(lambda d, r: _with(d, COPY=r.token)),
        {"test_hands_the_token_through_the_environment_only"},
    ),
    "a token that never arrives": (
        lambda: _EngineVariant(lambda d, r: _without(d, "ZEPHYROS_TOKEN")),
        {
            "test_hands_the_token_through_the_environment_only",
            "test_depends_on_the_token_of_the_request",
        },
    ),
    "an endpoint that never arrives": (
        lambda: _EngineVariant(lambda d, r: _without(d, "ZEPHYROS_ENDPOINT")),
        {"test_hands_the_agent_endpoint_and_the_model_to_the_turn"},
    ),
    "a model that never arrives": (
        lambda: _EngineVariant(lambda d, r: _without(d, "ZEPHYROS_MODEL")),
        {"test_hands_the_agent_endpoint_and_the_model_to_the_turn"},
    ),
    "a token frozen at the first request": (
        lambda: _EngineVariant(_stale_token()),
        {"test_depends_on_the_token_of_the_request"},
    ),
    "a prompt split into words": (
        lambda: _EngineVariant(lambda d, r: replace(d, command=("echo", *r.prompt.split()))),
        {"test_keeps_the_prompt_whole_in_one_word"},
    ),
    "a prompt that is dropped": (
        lambda: _EngineVariant(lambda d, r: replace(d, command=("echo", "fixed"))),
        {"test_keeps_the_prompt_whole_in_one_word"},
    ),
    "a variable name that is not portable": (
        lambda: _EngineVariant(lambda d, r: _with(d, **{"bad name": "x"})),
        {"test_names_variables_portably_and_holds_no_nul"},
    ),
    "a NUL in a value": (
        lambda: _EngineVariant(lambda d, r: _with(d, X="a\x00b")),
        {"test_names_variables_portably_and_holds_no_nul"},
    ),
    "a NUL in a word": (
        lambda: _EngineVariant(lambda d, r: replace(d, command=(*d.command, "a\x00"))),
        {"test_names_variables_portably_and_holds_no_nul"},
    ),
}


class TestAgentEngineSuite:
    @pytest.mark.parametrize("name", ENGINE_VARIANTS)
    def test_the_suite_catches(self, name: str) -> None:
        make, expected = ENGINE_VARIANTS[name]

        class Suite(AgentEngineConformance):
            def make_engine(self) -> AgentEngine:
                return make()

        assert failed_checks(Suite()) == expected

    def test_every_check_is_caught_by_some_variant(self) -> None:
        caught = set().union(*(expected for _, expected in ENGINE_VARIANTS.values()))
        assert caught == all_checks(AgentEngineConformance())


# --------------------------------------------------------------------------- model provider

SCRIPT = ChatMessage(
    Role.ASSISTANT, "ok", tool_calls=(ToolCall("call_1", "echo", '{"text": "hi"}'),)
)
Transform = Callable[[ModelRequest, ModelResponse], ModelResponse]


def _answer() -> ModelResponse:
    return ModelResponse(model="m", message=SCRIPT, usage=Usage(1, 1))


class _ProviderVariant(ScriptedModelProvider):
    def __init__(
        self, transform: Transform, *, interface: str = "0.1.0", refuse_empty: bool = True
    ) -> None:
        super().__init__([SCRIPT])
        self._transform = transform
        self._interface = interface
        self._refuse_empty = refuse_empty

    @property
    def interface_version(self) -> str:
        return self._interface

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not request.messages and not self._refuse_empty:
            return _answer()
        return self._transform(request, await super().complete(request))


def _with_message(message: ChatMessage) -> Transform:
    return lambda request, response: replace(response, message=message)


def _mutating(request: ModelRequest, response: ModelResponse) -> ModelResponse:
    cast(dict[str, object], request.tools[0].parameters)["injected"] = True
    return response


PROVIDER_VARIANTS: dict[str, tuple[Callable[[], ModelProvider], set[str]]] = {
    "a later major version": (
        lambda: _ProviderVariant(lambda q, r: r, interface="1.0.0"),
        {"test_follows_the_tested_major_version"},
    ),
    "an answer without a model name": (
        lambda: _ProviderVariant(lambda q, r: replace(r, model="")),
        {"test_answers_and_names_the_model"},
    ),
    "no prompt tokens counted": (
        lambda: _ProviderVariant(lambda q, r: replace(r, usage=Usage(0, 5))),
        {"test_reports_usage_for_a_non_empty_exchange"},
    ),
    "no completion tokens counted": (
        lambda: _ProviderVariant(lambda q, r: replace(r, usage=Usage(5, 0))),
        {"test_reports_usage_for_a_non_empty_exchange"},
    ),
    "an empty conversation that gets an answer": (
        lambda: _ProviderVariant(lambda q, r: r, refuse_empty=False),
        {"test_refuses_an_empty_conversation"},
    ),
    "a tool that was not offered": (
        lambda: _ProviderVariant(
            _with_message(ChatMessage(Role.ASSISTANT, "ok", (ToolCall("c1", "rm", "{}"),)))
        ),
        {"test_calls_only_tools_that_were_offered_with_json_arguments"},
    ),
    "arguments that are not JSON": (
        lambda: _ProviderVariant(
            _with_message(ChatMessage(Role.ASSISTANT, "ok", (ToolCall("c1", "echo", "not json"),)))
        ),
        {"test_calls_only_tools_that_were_offered_with_json_arguments"},
    ),
    "arguments that are not an object": (
        lambda: _ProviderVariant(
            _with_message(ChatMessage(Role.ASSISTANT, "ok", (ToolCall("c1", "echo", "[1]"),)))
        ),
        {"test_calls_only_tools_that_were_offered_with_json_arguments"},
    ),
    "tool calls that share an identifier": (
        lambda: _ProviderVariant(
            _with_message(
                ChatMessage(
                    Role.ASSISTANT, None, (ToolCall("c", "echo", "{}"), ToolCall("c", "echo", "{}"))
                )
            )
        ),
        {"test_calls_only_tools_that_were_offered_with_json_arguments"},
    ),
    "a tool call without identifier": (
        lambda: _ProviderVariant(
            _with_message(ChatMessage(Role.ASSISTANT, "ok", (ToolCall("", "echo", "{}"),)))
        ),
        {"test_calls_only_tools_that_were_offered_with_json_arguments"},
    ),
    "a request that is modified": (
        lambda: _ProviderVariant(_mutating),
        {"test_leaves_the_request_unchanged"},
    ),
}


class TestModelProviderSuite:
    @pytest.mark.parametrize("name", PROVIDER_VARIANTS)
    def test_the_suite_catches(self, name: str) -> None:
        make, expected = PROVIDER_VARIANTS[name]

        class Suite(ModelProviderConformance):
            async def make_provider(self) -> ModelProvider:
                return make()

        assert failed_checks(Suite()) == expected

    def test_every_check_is_caught_by_some_variant(self) -> None:
        caught = set().union(*(expected for _, expected in PROVIDER_VARIANTS.values()))
        assert caught == all_checks(ModelProviderConformance())


# ------------------------------------------------------------------------------------ forge

REPO = RepositoryRef("org/project")


def _fresh() -> InMemoryForge:
    return InMemoryForge({"org/project": frozenset({"main", "feature"})})


def _made_up(request: PullRequestRequest, number: int = 1) -> PullRequest:
    return PullRequest(
        request.repository, number, f"https://forge.invalid/x/{number}", request.head,
        request.base, request.title, request.body,
    )  # fmt: skip


class _ForgeVariant(InMemoryForge):
    def __init__(self, *, interface: str = "0.2.0") -> None:
        super().__init__({"org/project": frozenset({"main", "feature"})})
        self._interface = interface

    @property
    def interface_version(self) -> str:
        return self._interface


class _TitleChanged(_ForgeVariant):
    async def open_pull_request(self, request: PullRequestRequest) -> PullRequest:
        opened = await super().open_pull_request(request)
        return replace(opened, title=opened.title.upper())


class _BodyMangled(_ForgeVariant):
    async def open_pull_request(self, request: PullRequestRequest) -> PullRequest:
        return await super().open_pull_request(
            replace(request, body=request.body.replace("<", "&lt;"))
        )


class _ReadsBackAnother(_ForgeVariant):
    async def get_pull_request(self, repository: RepositoryRef, number: int) -> PullRequest:
        return replace(await super().get_pull_request(repository, number), head="main")


class _SameNumber(_ForgeVariant):
    async def open_pull_request(self, request: PullRequestRequest) -> PullRequest:
        opened = await super().open_pull_request(request)
        return replace(opened, number=1, url="https://forge.invalid/org/project/pull/1")


class _AcceptsEmptyTitle(_ForgeVariant):
    async def open_pull_request(self, request: PullRequestRequest) -> PullRequest:
        if not request.title.strip():
            return _made_up(request)
        return await super().open_pull_request(request)


class _AcceptsUnknownRepository(_ForgeVariant):
    async def open_pull_request(self, request: PullRequestRequest) -> PullRequest:
        if request.repository != REPO:
            return _made_up(request)
        return await super().open_pull_request(request)


class _AcceptsUnknownBranch(_ForgeVariant):
    async def open_pull_request(self, request: PullRequestRequest) -> PullRequest:
        if request.head not in {"main", "feature"}:
            return _made_up(request)
        return await super().open_pull_request(request)


class _InventsPullRequests(_ForgeVariant):
    async def get_pull_request(self, repository: RepositoryRef, number: int) -> PullRequest:
        try:
            return await super().get_pull_request(repository, number)
        except ForgeError:
            return _made_up(PullRequestRequest(repository, "feature", "main", "t", ""), number)


class _FetchesAnything(_ForgeVariant):
    async def fetch(self, repository: RepositoryRef, branch: str) -> bytes:
        del repository, branch
        return b"anything"


class _ForgetsPushes(_ForgeVariant):
    async def push(self, repository: RepositoryRef, branch: str, package: bytes) -> None:
        del repository, branch, package


class _RewritesBranches(_ForgeVariant):
    async def push(self, repository: RepositoryRef, branch: str, package: bytes) -> None:
        if repository == REPO and branch in {"main", "feature"}:
            return
        await super().push(repository, branch, package)


FORGE_VARIANTS: dict[str, tuple[Callable[[], Forge], set[str]]] = {
    "a later major version": (
        lambda: _ForgeVariant(interface="1.0.0"),
        {"test_follows_the_tested_major_version"},
    ),
    "a pull request that does not echo the request": (
        _TitleChanged,
        {"test_opens_a_pull_request_and_reads_it_back"},
    ),
    "a pull request read back differently": (
        _ReadsBackAnother,
        {"test_opens_a_pull_request_and_reads_it_back"},
    ),
    "a body that is changed": (_BodyMangled, {"test_keeps_the_body_verbatim"}),
    "pull requests that share a number": (
        _SameNumber,
        {"test_gives_each_pull_request_its_own_number"},
    ),
    "an empty title that is accepted": (_AcceptsEmptyTitle, {"test_refuses_an_empty_title"}),
    "an unknown repository that is accepted": (
        _AcceptsUnknownRepository,
        {"test_refuses_an_unknown_repository"},
    ),
    "an unknown branch that is accepted": (
        _AcceptsUnknownBranch,
        {"test_refuses_an_unknown_branch"},
    ),
    "a pull request that is invented": (
        _InventsPullRequests,
        {"test_refuses_an_unknown_pull_request"},
    ),
    "a fetch of anything that answers": (
        _FetchesAnything,
        {"test_fetches_an_existing_branch_and_refuses_an_unknown_one"},
    ),
    "a push that is lost": (
        _ForgetsPushes,
        {
            "test_a_pushed_branch_can_be_fetched_and_proposed",
            "test_never_rewrites_an_existing_branch",
        },
    ),
    "a push over an existing branch": (
        _RewritesBranches,
        {"test_never_rewrites_an_existing_branch"},
    ),
}


class TestForgeSuite:
    @pytest.mark.parametrize("name", FORGE_VARIANTS)
    def test_the_suite_catches(self, name: str) -> None:
        make, expected = FORGE_VARIANTS[name]

        class Suite(ForgeConformance):
            async def make_fixture(self) -> ForgeFixture:
                return ForgeFixture(make(), REPO, head="feature", base="main", package=b"p")

        assert failed_checks(Suite()) == expected

    def test_every_check_is_caught_by_some_variant(self) -> None:
        caught = set().union(*(expected for _, expected in FORGE_VARIANTS.values()))
        assert caught == all_checks(ForgeConformance())


# --------------------------------------------------------------------------------- delivery


class _AreaVariant(InMemoryDeliveryArea):
    def __init__(self, *, interface: str = "0.1.0") -> None:
        super().__init__()
        self._interface = interface

    @property
    def interface_version(self) -> str:
        return self._interface


class _MangledBase(_AreaVariant):
    def tree(self, project_id: str, ref: str) -> Tree:
        found = super().tree(project_id, ref)
        return Tree({**found.files, "extra": b"x"}) if ref == "base" else found


class _NoChangedPaths(_AreaVariant):
    def receive(self, project_id: str, package: bytes, branch: str) -> Delivery:
        return replace(super().receive(project_id, package, branch), changed_paths=())


class _OneIdentifier(_AreaVariant):
    def receive(self, project_id: str, package: bytes, branch: str) -> Delivery:
        return replace(super().receive(project_id, package, branch), delivery_id="d1")


class _AnyBranchName(_AreaVariant):
    def receive(self, project_id: str, package: bytes, branch: str) -> Delivery:
        return super().receive(project_id, package, "x" + str(abs(hash(branch)) % 10**9))


class _DeliversTwice(_AreaVariant):
    def receive(self, project_id: str, package: bytes, branch: str) -> Delivery:
        try:
            return super().receive(project_id, package, branch)
        except DeliveryError:
            return super().receive(project_id, package, branch + "-again")


class _AcceptsStrangers(_AreaVariant):
    def receive(self, project_id: str, package: bytes, branch: str) -> Delivery:
        data = json.loads(package) if package.startswith(b"{") else None
        if data is not None:
            data["base"] = self._base_ids.get(project_id)
            package = json.dumps(data).encode()
        return super().receive(project_id, package, branch)


class _InventsDeliveries(_AreaVariant):
    def get(self, project_id: str, delivery_id: str) -> Delivery:
        try:
            return super().get(project_id, delivery_id)
        except DeliveryError:
            return Delivery(project_id, delivery_id, "x", "b", "h", ())

    def tree(self, project_id: str, ref: str) -> Tree:
        try:
            return super().tree(project_id, ref)
        except DeliveryError:
            return Tree({})

    def receive(self, project_id: str, package: bytes, branch: str) -> Delivery:
        if project_id not in self._bases:
            self.prepare(project_id, make_package({}), "main")
            return Delivery(project_id, "d0", branch, "b", "h", ())
        return super().receive(project_id, package, branch)


DELIVERY_VARIANTS: dict[str, tuple[Callable[[], DeliveryArea], set[str]]] = {
    "a later major version": (
        lambda: _AreaVariant(interface="1.0.0"),
        {"test_follows_the_tested_major_version"},
    ),
    "a base that is changed": (_MangledBase, {"test_keeps_the_base_as_it_was_given"}),
    "changed paths that are lost": (
        _NoChangedPaths,
        {
            "test_keeps_a_delivery_with_its_changed_paths_and_its_tree",
            "test_gives_each_delivery_its_own_identifier",
        },
    ),
    "deliveries that share an identifier": (
        _OneIdentifier,
        {"test_gives_each_delivery_its_own_identifier"},
    ),
    "any branch name": (
        _AnyBranchName,
        {
            "test_refuses_a_branch_name_that_is_not_plain",
            "test_keeps_a_delivery_with_its_changed_paths_and_its_tree",
        },
    ),
    "a branch delivered twice": (_DeliversTwice, {"test_refuses_a_branch_delivered_twice"}),
    "a stranger accepted": (
        _AcceptsStrangers,
        {"test_refuses_what_does_not_descend_from_the_base"},
    ),
    "deliveries that are invented": (
        _InventsDeliveries,
        {"test_refuses_an_unknown_project_or_delivery"},
    ),
}


class TestDeliverySuite:
    @pytest.mark.parametrize("name", DELIVERY_VARIANTS)
    def test_the_suite_catches(self, name: str) -> None:
        make, expected = DELIVERY_VARIANTS[name]

        class Suite(DeliveryConformance):
            def make_fixture(self) -> DeliveryFixture:
                return DeliveryFixture(make(), derive_dummy)

        assert failed_checks(Suite()) == expected

    def test_every_check_is_caught_by_some_variant(self) -> None:
        caught = set().union(*(expected for _, expected in DELIVERY_VARIANTS.values()))
        assert caught == all_checks(DeliveryConformance())


# ----------------------------------------------------------------------------- verification


class _RunnerVariant(AssertionRunner):
    def __init__(self, *, interface: str = "0.1.0", kits: frozenset[str] | None = None) -> None:
        self._interface = interface
        self._kits = frozenset({"assertions"}) if kits is None else kits
        self.calls = 0

    @property
    def interface_version(self) -> str:
        return self._interface

    @property
    def kits(self) -> frozenset[str]:
        return self._kits


class _AllPass(_RunnerVariant):
    def run(self, request: VerificationRequest) -> VerificationResult:
        return VerificationResult(dict.fromkeys(request.tests, Outcome.PASSED))


class _ExtraOutcomes(_RunnerVariant):
    def run(self, request: VerificationRequest) -> VerificationResult:
        found = super().run(request)
        return VerificationResult({**found.outcomes, "other::test": Outcome.PASSED})


class _BrokenPasses(_RunnerVariant):
    def run(self, request: VerificationRequest) -> VerificationResult:
        found = super().run(request)
        mapped = {t: Outcome.PASSED if o is Outcome.ERROR else o for t, o in found.outcomes.items()}
        return VerificationResult(mapped)


class _Drifting(_RunnerVariant):
    def run(self, request: VerificationRequest) -> VerificationResult:
        self.calls += 1
        found = super().run(request)
        if self.calls == 1:
            return found
        return VerificationResult(dict.fromkeys(found.outcomes, Outcome.NOT_RUN))


class _AnyKit(_RunnerVariant):
    def run(self, request: VerificationRequest) -> VerificationResult:
        return super().run(replace(request, kit="assertions"))


RUNNER_VARIANTS: dict[str, tuple[Callable[[], VerificationRunner], set[str]]] = {
    "a later major version": (
        lambda: _RunnerVariant(interface="1.0.0"),
        {"test_follows_the_tested_major_version"},
    ),
    "no kit named": (lambda: _RunnerVariant(kits=frozenset()), {"test_names_the_kit_it_runs"}),
    "everything passes": (
        _AllPass,
        {
            "test_tells_a_passing_test_from_a_failing_one",
            "test_a_broken_test_never_passes",
            "test_gives_one_outcome_to_each_test_asked_for_and_to_no_other",
            "test_refuses_a_kit_it_does_not_run",
        },
    ),
    "outcomes for tests not asked for": (
        _ExtraOutcomes,
        {"test_gives_one_outcome_to_each_test_asked_for_and_to_no_other"},
    ),
    "a broken test that passes": (_BrokenPasses, {"test_a_broken_test_never_passes"}),
    "outcomes that drift": (_Drifting, {"test_gives_the_same_outcomes_twice"}),
    "any kit run": (_AnyKit, {"test_refuses_a_kit_it_does_not_run"}),
}


class TestVerificationSuite:
    @pytest.mark.parametrize("name", RUNNER_VARIANTS)
    def test_the_suite_catches(self, name: str) -> None:
        make, expected = RUNNER_VARIANTS[name]

        class Suite(VerificationConformance):
            def make_fixture(self) -> VerificationFixture:
                return VerificationFixture(make(), "assertions", write_assertions)

        assert failed_checks(Suite()) == expected

    def test_every_check_is_caught_by_some_variant(self) -> None:
        caught = set().union(*(expected for _, expected in RUNNER_VARIANTS.values()))
        assert caught == all_checks(VerificationConformance())
