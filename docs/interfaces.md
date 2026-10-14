# Public interfaces

The hub reaches the agent engine, the model provider, the forge, the delivery area and the verification runner through five public interfaces (CTR-11, US-E11-06). Each carries a version number, has a dummy implementation, and has a conformance suite that every implementation passes. The code is in `src/zephyros/core/ports/`, the dummies in `src/zephyros/adapters/dummy/`, the suites in `src/zephyros/conformance/`.

## Stability

All five interfaces are at version 0.x. Until 1.0, a change can break an implementation, and the change is announced in the commit that makes it.

- **AgentEngine** and **ModelProvider** may change after spike Z2-01, which settles how the pinned Hermes Agent takes its configuration and its token, and whether it needs streaming answers. Nothing here assumes the answer.
- **Forge**, **DeliveryArea** and **VerificationRunner** carry the first slice of the delivery loop (Z3). They will grow with it: more kits, the evidence a kit returns. The validation of repository sources is in: a project source must match annex "Accepted repository sources" (`core/sources.py`).

## Implementing an interface

Write a class with the operations below and a read-only `interface_version`. Then check it:

```python
from zephyros.conformance import ForgeConformance, ForgeFixture


class TestMyForge(ForgeConformance):
    async def make_fixture(
        self,
    ) -> ForgeFixture: ...  # a forge and a repository with two existing branches
```

`pytest` collects the checks. Each suite builds what it tests inside the event loop of the check and closes it there. The suite fails with `ConformanceError`, never with `assert`, so that `python -O` cannot switch it off. In this repository, `tests/unit/conformance/implementations.py` lists one conformance run for each implementation under `zephyros.adapters`, and a test fails when one is missing.

## AgentEngine

Version: `0.2.0`

The agent engine is the software that runs an agent in an instance (Hermes Agent in version 1). Each piece of work is one *turn*: one container started from the configured instance image, in the network namespace of the instance's guard, with the instance's two volumes (ADR 0016). The container filter builds that container from its own templates (ADR 0014); the engine only says which command and which environment start a turn. A description therefore holds no image, user, mount, capability, network, limit or file.

| Operation | Meaning |
| --- | --- |
| `interface_version` | The version of this interface that the implementation follows |
| `engine_version` | The version of the agent engine that instances run |
| `describe_turn(TurnRequest) -> TurnDescription` | The words of the command and the environment variables that start a turn |

`TurnRequest` holds the project, the side (`owner` or `contractor`), the URL of the hub's agent endpoint, the model name, the project token and the prompt of the turn. The token and the prompt do not appear when the request is printed.

The filter checks every description before using it, as if it came from outside: no empty word, no NUL, the token never on the command line and present in the environment, plain variable names.

What the suite checks: the major version; a non-empty engine version; a non-empty command with no empty word; the same request gives the same description; the token reaches the turn through exactly one environment variable and never on the command line; a new token replaces the old one; the agent endpoint and the model reach the turn; a hostile prompt stays whole in one word of the command; portable variable names and no NUL.

Dummy: `DummyAgentEngine`, a turn that echoes its prompt. Real: `HermesEngine` (`hermes chat --query=<prompt> --oneshot --continue=zephyros --create-if-missing --quiet`: a one-shot chat in the named session of the instance, created by the first turn).

Changes from 0.1: `describe_instance` became `describe_turn`; the image is configured, not described; the files are gone (the configuration of Hermes is in the instance image, seeded from the environment at each start).

## ModelProvider

Version: `0.1.0`

A model provider answers a chat conversation, possibly with calls to tools that the caller offered. The hub's model gateway translates the wire format of an instance into these types. The provider never sees a project token.

| Operation | Meaning |
| --- | --- |
| `interface_version` | The version of this interface that the implementation follows |
| `complete(ModelRequest) -> ModelResponse` | Answer the conversation. Raises `ModelProviderError` when it cannot or must not |
| `aclose()` | Release connections and files, once, in the event loop that used the provider |

What the suite checks: the major version; the answer names a model; the usage counts both prompt and completion tokens; an empty conversation is refused with `ModelProviderError`; every tool call in the answer has an identifier, is unique, names a tool that the request offered, and carries a JSON object as arguments; the request is not modified.

Dummy: `ScriptedModelProvider`, which replays replies written in advance and records the requests. It is the seed of the simulated model provider (EXG-46, Z3) and of the obedient stub that the attack suite uses.

## Forge

Version: `0.4.0`

A forge hosts git repositories and pull requests (GitHub in version 1). It opens a pull request and reads it back, comments on it, retitles it, fetches a branch as a package and pushes a branch from one. A package is in the format of the delivery area (a git bundle for the default one).

| Operation | Meaning |
| --- | --- |
| `interface_version` | The version of this interface that the implementation follows |
| `open_pull_request(PullRequestRequest) -> PullRequest` | Raises `ForgeError` for an empty title, an unknown repository or an unknown branch |
| `get_pull_request(RepositoryRef, number) -> PullRequest` | Raises `ForgeError` if there is none |
| `comment(RepositoryRef, number, body)` | Add a comment, kept verbatim; `ForgeError` for an unknown pull request or an empty body (0.3: what the Owner side records after the gate, ADR 0020) |
| `update_title(RepositoryRef, number, title)` | Change the title; `ForgeError` for an unknown pull request or an empty title (0.4: the hub retitles a pull request when an inspection changes the decision, ADR 0020) |
| `fetch(RepositoryRef, branch) -> bytes` | The branch as a package; `ForgeError` for an unknown repository or branch |
| `push(RepositoryRef, branch, package)` | Create the branch; an existing branch is refused (the hub never rewrites a branch of the user) |
| `aclose()` | Release what the forge holds, once, in the event loop that used it |

Implementations: `InMemoryForge` (dummy), `DirectoryForge` (bare git repositories in a directory, for the development deployment and the tests), `GitHubForge` (the repositories of the person: its API for pull requests, git over https for branches; the token is read from its file at each call, travels through a credential helper that reads the file itself, and never appears in a command line, a message or a log record).

The body of a pull request is text. The forge returns it exactly as it received it, markup and control characters included, and never interprets it (ATK-60 relies on this).

What the suite checks: the major version; a pull request echoes the request and reads back unchanged; a hostile body comes back verbatim; each pull request has its own number and address; an empty title, an unknown repository, an unknown branch and an unknown pull request are refused; a comment is added and an unknown pull request and an empty comment are refused; a pull request is retitled and an unknown one and an empty title are refused; an existing branch can be fetched and an unknown one cannot; a pushed branch can be fetched and proposed; an existing branch is never pushed over.

Dummy: `InMemoryForge`, repositories, packages and pull requests in a dictionary.

## DeliveryArea

Version: `0.1.0`

The staging area of the hub: where the deliveries of a project are kept, and how a tree is read back. A delivery is the work the Contractor pushes and declares; the core speaks of deliveries, bases and trees, never of how they are stored. The default implementation keeps each delivery as a git branch in a bare repository of the hub, and a package is a git bundle.

| Operation | Meaning |
| --- | --- |
| `interface_version` | The version of this interface that the implementation follows |
| `prepare(project_id, package, branch) -> str` | Make that branch of the package the base of the project; returns the base identifier |
| `base_package(project_id) -> bytes` | The base, as a package an instance can start from |
| `receive(project_id, package, branch) -> Delivery` | Keep a pushed branch as a new delivery, with its base, its head and the paths it changes |
| `get(project_id, delivery_id) -> Delivery` | A delivery kept earlier |
| `tree(project_id, ref) -> Tree` | The files of the base (`"base"`) or of a delivery |
| `package(project_id, delivery_id) -> bytes` | A delivery as a package, to push it to the forge |

What the suite checks: the major version; the base is kept as given; a delivery keeps its branch name, its changed paths and its tree; each delivery has its own identifier; a branch name that is not plain, a branch delivered twice, a state that does not descend from the base, an unreadable package, an unknown project and an unknown delivery are refused (`DeliveryError`).

Dummy: `InMemoryDeliveryArea`, trees in a dictionary and packages in a small JSON format.

## VerificationRunner

Version: `0.2.0`

What starts the project under verification and observes it, for one verification kit (ADR 0017). Given the files to verify and the acceptance tests to run, it returns one outcome per test (`passed`, `failed`, `error`, `not_run`) with bounded evidence: a log cut to 20 000 characters, and files such as screenshots. It also returns `deselected`: the tests the kit found in the asked files but did not run because no acceptance criterion named them, so the report can say which acceptance tests never ran. It renders no decision; the gate does.

Every run also carries **canary tests of the hub** (two, named at random) that must fail (ATK-22): the delivery shares the sandbox with the kit, so a conftest that marks every test passed, an early exit or a monkeypatched framework could otherwise forge the result. A run whose canary did not come back failed is not believed: the verification fails with an error. The kit runs with the working directory off `sys.path` (`python -P`), so a file of the delivery cannot shadow the kit plugin it is judged by.

| Operation | Meaning |
| --- | --- |
| `interface_version` | The version of this interface that the implementation follows |
| `kits` | The kits this runner can run |
| `run(VerificationRequest) -> VerificationResult` | Run the acceptance tests of the request with its kit; `VerificationError` for a kit the runner does not run |

`VerificationResult` carries `outcomes` (one per test asked for), `log`, `evidence`, `duration_seconds` and `deselected` (tests seen in the asked files but not named by a criterion; none is a test asked for).

What the suite checks: the major version; the runner names the kit it runs; a passing test passes and a failing one fails; a broken test never passes; every test asked for gets exactly one outcome and no other test gets one, an absent test does not pass, and the deselected tests are well-formed and never shadow a test asked for; the same request gives the same outcomes twice; an unknown kit is refused.

Dummy: `AssertionRunner`, whose one kit ("assertions") runs no code: a test `path::name` passes when the file holds the line `name: pass`.
