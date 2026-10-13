"""Conformance suite of the model provider interface.

To check an implementation, subclass `ModelProviderConformance`, implement `make_provider`
and, if the provider needs a prepared answer, `request`. The provider is built inside the
event loop of each test and closed there.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable

from zephyros.conformance._support import (
    ConformanceError,
    check,
    check_interface_version,
    run_in_loop,
)
from zephyros.core.ports.model_provider import (
    INTERFACE_VERSION,
    ChatMessage,
    ModelProvider,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    Role,
    ToolSpec,
)


class ModelProviderConformance:
    """Rules every model provider follows."""

    model = "conformance-model"

    async def make_provider(self) -> ModelProvider:
        raise NotImplementedError

    def request(self) -> ModelRequest:
        """A conversation that offers one tool named "echo"."""
        return ModelRequest(
            model=self.model,
            messages=(
                ChatMessage(Role.SYSTEM, "You are a test."),
                ChatMessage(Role.USER, "Call the echo tool with the text hi."),
            ),
            tools=(
                ToolSpec(
                    name="echo",
                    description="Repeat a text.",
                    parameters={"type": "object", "properties": {"text": {"type": "string"}}},
                ),
            ),
        )

    def _with_provider(self, scenario: Callable[[ModelProvider], Awaitable[None]]) -> None:
        async def main() -> None:
            provider = await self.make_provider()
            try:
                await scenario(provider)
            finally:
                await provider.aclose()

        run_in_loop(main)

    async def _answer(self, provider: ModelProvider) -> ModelResponse:
        response = await provider.complete(self.request())
        check(isinstance(response, ModelResponse), "not a ModelResponse")
        return response

    def test_follows_the_tested_major_version(self) -> None:
        async def scenario(provider: ModelProvider) -> None:
            check_interface_version(provider.interface_version, INTERFACE_VERSION)

        self._with_provider(scenario)

    def test_answers_and_names_the_model(self) -> None:
        async def scenario(provider: ModelProvider) -> None:
            response = await self._answer(provider)
            check(response.model, "the answer names no model")

        self._with_provider(scenario)

    def test_reports_usage_for_a_non_empty_exchange(self) -> None:
        async def scenario(provider: ModelProvider) -> None:
            usage = (await self._answer(provider)).usage
            check(usage.prompt_tokens > 0, "no prompt tokens reported for a non-empty prompt")
            check(usage.completion_tokens > 0, "no completion tokens reported for an answer")

        self._with_provider(scenario)

    def test_refuses_an_empty_conversation(self) -> None:
        async def scenario(provider: ModelProvider) -> None:
            try:
                await provider.complete(ModelRequest(model=self.model, messages=()))
            except ModelProviderError:
                return
            raise ConformanceError("an empty conversation was answered")

        self._with_provider(scenario)

    def test_calls_only_tools_that_were_offered_with_json_arguments(self) -> None:
        async def scenario(provider: ModelProvider) -> None:
            request = self.request()
            offered = {tool.name for tool in request.tools}
            calls = (await self._answer(provider)).message.tool_calls
            check(len({call.id for call in calls}) == len(calls), "tool call identifiers repeat")
            for call in calls:
                check(call.id, "a tool call has no identifier")
                check(call.name in offered, f"the answer calls a tool not offered: {call.name!r}")
                try:
                    arguments = json.loads(call.arguments)
                except json.JSONDecodeError:
                    raise ConformanceError("tool call arguments are not JSON") from None
                check(isinstance(arguments, dict), "tool call arguments are not a JSON object")

        self._with_provider(scenario)

    def test_leaves_the_request_unchanged(self) -> None:
        async def scenario(provider: ModelProvider) -> None:
            request = self.request()
            before = copy.deepcopy(request)
            await provider.complete(request)
            check(request == before, "the request was modified")

        self._with_provider(scenario)
