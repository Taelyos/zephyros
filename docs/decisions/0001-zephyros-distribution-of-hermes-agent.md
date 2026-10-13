# 0001. Zephyros is a distribution of Hermes Agent, not a new engine
- Status: Accepted
- Date: 2026-10-09

## Context
Zephyros brings project engineering to AI agents. A new agent engine would have to re-create what an existing open-source agent already offers: profiles, delegation to sub-agents in git worktrees, messaging, cron jobs and model providers. Our differentiation lies in how work is organized and verified, not in the engine itself.

## Decision
Zephyros is a distribution of Hermes Agent (MIT, Nous Research), pinned to a known version. Hermes supplies the engine. Zephyros adds the method, the hub, a dashboard and a one-command installer.

## Consequences
- Positive: the engine features listed above come from an existing project, so our effort goes into the method and the hub.
- Positive: pinning a known version keeps behavior predictable between upgrades.
- Positive: the project stays a thin layer with a small surface to maintain.
- Negative: Hermes moves fast, so each upgrade must be deliberate and pass a non-regression test.

## Alternatives considered
- Build our own agent runtime.
- Build on AgentCraft's Foreman (see [0010](0010-agentcraft-foreman-not-used.md)).
