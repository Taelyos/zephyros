# 0003. One owner instance and one contractor instance per project, plus a hub without any LLM
- Status: Accepted; in part replaced by [0016](0016-one-agent-container-per-turn.md)
- Date: 2026-10-09
- Replaced in part: instances are not woken by signed webhooks and send no webhooks. Each
  instance is a network guard and two volumes, and the hub starts one agent container per turn
  ([0016](0016-one-agent-container-per-turn.md)). Signed webhooks go from the hub to the main
  agent only ([notifications](../notifications.md)). The rest of this decision stands.

## Context
Owner and contractor roles need rights that do not depend on model behavior. Mixing both roles, or sharing instances across projects, would rely on profile rules.

## Decision
Each project has one owner instance and one contractor instance. A hub without any LLM holds the contract, git staging area, verification results, gate, rights and events, and manages project instances. Agents reach the hub over MCP (Streamable HTTP) with a per-instance token carrying their side and project. Signed webhooks wake instances; activity flows back through signed outbound webhooks. The contract is never mounted in an agent container. A permanent main agent, any MCP-capable agent, creates projects and talks to the human.

## Consequences
- Positive: rights rest on separate instances and tokens, not profile rules.
- Positive: the contract never enters a contractor container.
- Negative: new, security-critical hub code with filtered Docker access must stay small.
- Negative: each project needs more RAM, so instances start on demand.

## Alternatives considered
- A single Hermes instance with per-profile rules.
- Two long-lived instances shared by all projects.
