# 0010. AgentCraft's Foreman is not used
- Status: Accepted
- Date: 2026-10-09

## Context
The Foreman is tied to the Claude Agent SDK and supports no other providers. It is coupled to Minecraft in several places, has no system-level isolation and is very young. Hermes already delegates to sub-agents in git worktrees. Zephyros needs delegation that works with any provider and runs under OS-level isolation, and the Foreman offers neither.

## Decision
Zephyros does not use the Foreman. We credit AgentCraft and reuse its ideas, such as lead review followed by a human merge.

## Consequences
- Positive: no dependency on a single provider's SDK, and no game code to remove.
- Positive: delegation relies on Hermes' existing worktree mechanism.
- Negative: we give up an existing implementation and must build the review flow ourselves.
- Negative: we must document which ideas come from AgentCraft, so attribution stays accurate.

## Alternatives considered
- Fork the Foreman without the game.
