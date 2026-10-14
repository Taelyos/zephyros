# Architecture Decision Records

Architecture Decision Records (ADRs) record the significant design choices behind Zephyros, with their context, consequences and alternatives.

| No. | Title | Status |
| --- | --- | --- |
| 0001 | [Zephyros is a distribution of Hermes Agent, not a new engine](0001-zephyros-distribution-of-hermes-agent.md) | Accepted |
| 0002 | [Guarantees come from the operating system and the hub, never from prompts](0002-guarantees-from-os-and-hub.md) | Accepted |
| 0003 | [One owner instance and one contractor instance per project, plus a hub without any LLM](0003-owner-and-contractor-instances-with-hub.md) | Accepted |
| 0004 | [Delivered code runs only in a disposable sandbox launched by the hub](0004-sandboxed-execution-of-delivered-code.md) | Accepted |
| 0005 | [Judge each requirement, then decide acceptance by deterministic rules](0005-deterministic-acceptance-gate.md) | Accepted |
| 0006 | [The user's GitHub repository is the source of truth; v1 stops at an accepted pull request](0006-github-repository-as-source-of-truth.md) | Accepted |
| 0007 | [High-risk approvals are signed by the human with a passkey](0007-passkey-signed-high-risk-approvals.md) | Accepted |
| 0008 | [A model gateway in the hub; instances never hold provider credentials](0008-model-gateway-in-hub.md) | Accepted |
| 0009 | [Network access policy for instances and sandboxes](0009-network-access-policy.md) | Accepted |
| 0010 | [AgentCraft's Foreman is not used](0010-agentcraft-foreman-not-used.md) | Accepted |
| 0011 | [Draw on BMAD, do not build on it](0011-draw-on-bmad-not-build-on-it.md) | Accepted |
| 0012 | [Epics and user stories are written by the owner side; the contractor owns the solution](0012-owner-writes-epics-and-stories.md) | Accepted |
| 0013 | [Tokens are opaque, bound to their instance and checked against server-side records](0013-opaque-server-side-tokens.md) | Accepted |
| 0014 | [The container filter is a typed broker, not a pass-through proxy](0014-container-filter-as-typed-broker.md) | Accepted |
| 0015 | [Each instance has its own network and a network guard that enforces egress](0015-per-instance-networks-with-a-network-guard.md) | Accepted |
| 0016 | [An instance is its network guard and its volumes; each agent container is one turn](0016-one-agent-container-per-turn.md) | Proposed |
| 0017 | [A verification runner runs one verification kit; the method does not change with the project](0017-verification-runner-and-kits.md) | Proposed |
| 0018 | [An instance resolves names through configured resolvers, never through the host's resolver chain](0018-instance-name-resolution.md) | Proposed |
| 0019 | [An anchor for the log head, outside the store](0019-anchor-outside-the-store.md) | Proposed |
| 0020 | [The Owner side sees what it judges, and its verdict is recorded](0020-the-owner-sees-and-records.md) | Accepted; amendment Proposed |
| 0021 | [The continuity ladder waits until the contract is ready for the Contractor](0021-the-ladder-waits-for-a-ready-contract.md) | Proposed |
| 0023 | [A decision on the contract is awaited from whoever is to take it](0023-a-decision-is-awaited-from-whoever-takes-it.md) | Proposed |
