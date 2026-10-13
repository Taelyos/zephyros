"""US-E11-06 (CTR-11): each dummy implementation passes the conformance suite of its interface."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import ClassVar

from zephyros.adapters.dummy import DummyAgentEngine, InMemoryForge, ScriptedModelProvider
from zephyros.adapters.dummy.delivery import InMemoryDeliveryArea, make_package, state_id
from zephyros.adapters.dummy.obedient_stub import ObedientStubProvider
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
from zephyros.core.ports.agent_engine import AgentEngine
from zephyros.core.ports.forge import RepositoryRef
from zephyros.core.ports.model_provider import ChatMessage, ModelProvider, Role, ToolCall


class TestDummyAgentEngine(AgentEngineConformance):
    implementation: ClassVar[type] = DummyAgentEngine

    def make_engine(self) -> AgentEngine:
        return DummyAgentEngine()


class TestScriptedModelProvider(ModelProviderConformance):
    implementation: ClassVar[type] = ScriptedModelProvider

    async def make_provider(self) -> ModelProvider:
        reply = ChatMessage(
            Role.ASSISTANT, "ok", tool_calls=(ToolCall("call_1", "echo", '{"text": "hi"}'),)
        )
        return ScriptedModelProvider([reply])


class TestObedientStubProvider(ModelProviderConformance):
    implementation: ClassVar[type] = ObedientStubProvider

    async def make_provider(self) -> ModelProvider:
        return ObedientStubProvider()


class TestInMemoryForge(ForgeConformance):
    implementation: ClassVar[type] = InMemoryForge

    async def make_fixture(self) -> ForgeFixture:
        forge = InMemoryForge({"org/project": frozenset({"main", "feature"})})
        return ForgeFixture(
            forge, RepositoryRef("org/project"), head="feature", base="main", package=b"package"
        )


def derive_dummy(files: Mapping[str, bytes], parent: bytes | None) -> bytes:
    """A package of the in-memory area, descending from `parent`."""
    base = None
    if parent is not None:
        base = state_id({p: base64.b64decode(c) for p, c in json.loads(parent)["files"].items()})
    return make_package(files, base)


def write_assertions(behaviors: Mapping[str, str]) -> tuple[dict[str, bytes], dict[str, str]]:
    """Files of the "assertions" kit: `label: pass`, `label: fail`, or an unreadable answer."""
    answers = {"pass": "pass", "fail": "fail", "error": "maybe"}
    files = {
        f"checks/{label}.txt": f"{label}: {answers[b]}\n".encode() for label, b in behaviors.items()
    }
    names = {label: f"checks/{label}.txt::{label}" for label in behaviors}
    return files, names


class TestInMemoryDeliveryArea(DeliveryConformance):
    implementation: ClassVar[type] = InMemoryDeliveryArea

    def make_fixture(self) -> DeliveryFixture:
        return DeliveryFixture(InMemoryDeliveryArea(), derive_dummy)


class TestAssertionRunner(VerificationConformance):
    implementation: ClassVar[type] = AssertionRunner

    def make_fixture(self) -> VerificationFixture:
        return VerificationFixture(AssertionRunner(), "assertions", write_assertions)
