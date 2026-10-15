"""The explicit list of conformance runs: one test class per implementation under adapters.

US-E11-06 asks that the CI run the conformance suite against every real implementation.
`test_every_implementation_is_checked.py` fails when an implementation exists under
`zephyros.adapters` and is missing here. A real implementation that needs a container
runtime or a credential adds its test class here, marked `integration`.
"""

from __future__ import annotations

from collections.abc import Mapping

from tests.runtime.test_sandbox_runner_conforms import TestSandboxRunner
from tests.unit.conformance.test_dummies_conform import (
    TestAssertionRunner,
    TestDummyAgentEngine,
    TestInMemoryDeliveryArea,
    TestInMemoryForge,
    TestObedientStubProvider,
    TestScriptedModelProvider,
)
from tests.unit.conformance.test_real_implementations_conform import (
    TestDirectoryForge,
    TestGitDeliveryArea,
    TestGitHubForge,
    TestHermesEngine,
    TestOpenAiCompatibleProvider,
    TestOpenCodeEngine,
)
from zephyros.adapters.dummy import DummyAgentEngine, InMemoryForge, ScriptedModelProvider
from zephyros.adapters.dummy.delivery import InMemoryDeliveryArea
from zephyros.adapters.dummy.obedient_stub import ObedientStubProvider
from zephyros.adapters.dummy.verification import AssertionRunner
from zephyros.adapters.git.delivery import GitDeliveryArea
from zephyros.adapters.git.forge import DirectoryForge
from zephyros.adapters.github.forge import GitHubForge
from zephyros.adapters.hermes.engine import HermesEngine
from zephyros.adapters.opencode.engine import OpenCodeEngine
from zephyros.adapters.providers.openai_compatible import OpenAiCompatibleProvider
from zephyros.adapters.sandbox_runner import SandboxRunner

CHECKED: Mapping[type, type] = {
    GitHubForge: TestGitHubForge,
    DummyAgentEngine: TestDummyAgentEngine,
    ScriptedModelProvider: TestScriptedModelProvider,
    InMemoryForge: TestInMemoryForge,
    ObedientStubProvider: TestObedientStubProvider,
    OpenAiCompatibleProvider: TestOpenAiCompatibleProvider,
    HermesEngine: TestHermesEngine,
    OpenCodeEngine: TestOpenCodeEngine,
    InMemoryDeliveryArea: TestInMemoryDeliveryArea,
    AssertionRunner: TestAssertionRunner,
    GitDeliveryArea: TestGitDeliveryArea,
    DirectoryForge: TestDirectoryForge,
    SandboxRunner: TestSandboxRunner,
}
