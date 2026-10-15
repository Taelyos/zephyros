# 0016. An instance is its network guard and its volumes; each agent container is one turn

- Status: Accepted, 2026-10-14
- Decided by the project owner on 2026-10-14.
- Date: 2026-10-10
- Supersedes: the sentence "Signed webhooks wake instances; activity flows back through signed outbound webhooks" of [0003](0003-owner-and-contractor-instances-with-hub.md)

## Context
The hub has to give work to an instance, and the permitted container operations have no `exec` and no `attach`. ADR 0003 foresaw signed webhooks that wake an instance. A measurement of the pinned Hermes Agent showed what it offers. Run as `hermes -z "<prompt>"`, it opens no listener at all; a new container with `--continue` resumes the conversation kept on the home volume. Run as a gateway with its webhook platform, it opens a port and two Unix sockets and starts a cron scheduler and a task dispatcher inside the instance.

## Decision
- An **instance** is its network guard (which has the fixed address of the instance) and its two volumes, the Hermes home and the work volume. A project has one Owner instance and one Contractor instance.
- Each Hermes container is one **turn** of an instance. The hub starts it with the first prompt of the turn as the command, in the network namespace of the guard, with the same two volumes. Every turn continues one named session kept on the home volume, which the first turn creates. The turn ends when Hermes exits, and the container is removed. Nothing listens in an instance, and the hub never opens a connection to one.
- The project token is bound to the address of the guard. It lives as long as the instance, with an expiry longer than the longest allowed turn. It is revoked when the instance stops, not when a turn ends. The next turn is started with a new token when the current one nears its expiry.
- The agent calls the hub on its one endpoint. The hub reaches no agent.

## Consequences
- Positive: no inbound path at all, so the guard needs no inbound rule and ATK-76 has nothing to block but what the agent starts itself.
- Positive: no background work outlives a turn: when Hermes exits, the container stops with everything in it.
- Positive: token rotation has a natural point, the start of a turn.
- Negative: each turn pays the start of a container, about 5 to 10 s.
- Negative: the hub cannot interrupt a running turn except by stopping its container, and has to watch for a lost hub or a runaway turn itself, because Hermes does not stop by itself.
- Negative: webhooks as a way to wake an instance are gone. A later milestone can bring a gateway back as a new decision.

## Alternatives considered
- A long-running Hermes gateway with a webhook listener: works, but adds an inbound port, two Unix sockets and in-process scheduling inside every instance.
- Hermes Relay, an outbound connection to a connector service: documented as experimental, with a wire contract that may change.

## Amendment (Proposed, 2026-10-15): the operator can start a new session

A session that went wrong could not be left behind: every turn continued it, and the restarts of the continuity ladder too. A real run showed the case: an Owner whose conversation had been compacted fourteen times went on from a summary of a summary.

- `instance.start` takes `session`: `continue`, the default, or `new`. With `new`, the turn starts a new session of the instance, and every later turn continues that one. The earlier sessions stay on the home volume, unread.
- `new` is the operator's alone. Another caller asking for it is refused (`forbidden`): a conversation dropped is work lost. The restarts of the continuity ladder always continue.
- Each `turn.start` records `session`, `continue` or `new`. The `instance.start` of a new session records `session: new`.
- Hermes: the image's entrypoint first creates the next session of the lineage, `zephyros #N`. Hermes then resumes it for the name `zephyros`, as the latest of that name. OpenCode: the entrypoint runs `opencode run` without `--session` or `--continue`, so OpenCode creates a session, which the next turns find as the last one written.
- Nothing else changes: the volumes, the token and the guard of the instance stay. The work volume keeps the work of the old session.
