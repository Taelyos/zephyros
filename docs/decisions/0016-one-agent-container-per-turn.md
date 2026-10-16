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

- `instance.start` takes `session`: `continue`, the default, or `new`. With `new`, the turn starts a new session of the instance, and every later turn continues that one. The earlier sessions stay on the home volume. No turn continues them, but Hermes can still search them with its `session_search` tool.
- `new` is the operator's alone. Another caller asking for it is refused (`forbidden`): a conversation dropped is work lost. The restarts of the continuity ladder always continue.
- Each `turn.start` records `session`, `continue` or `new`. The `instance.start` of a new session records `session: new`.
- Hermes: the image's entrypoint first creates the next session of the lineage, `zephyros #N`. Hermes then resumes it for the name `zephyros`, as the latest of that name. OpenCode: the entrypoint runs `opencode run` without `--session` or `--continue`, so OpenCode creates a session, which the next turns find as the last one written.
- Nothing else changes: the volumes, the token and the guard of the instance stay. The work volume keeps the work of the old session. What the engine keeps beside the conversation stays too, and the new session reads it: the persistent memory of Hermes (`MEMORY.md`, `SOUL.md`) and its skills, on the home volume, and the `AGENTS.md` of the work directory. A new session therefore does not undo an instruction the agent wrote there, whether it misled itself or was misled; that was already true of the terminal, which could write those files. To start an instance from nothing, the operator stops it and removes its volumes.

## Amendment (Proposed, 2026-10-16): a token per turn, renewed while it runs

A real run of four and a half hours showed the token of an instance expire in the middle of a turn. The token was kept from one turn to the next and replaced only by a turn that started with less than `tokens.rotate_before_seconds` (15 minutes) left on it; it expired four hours after it was issued, whatever ran then. A turn that had started with an hour and a half left lost every tool of the hub halfway through, `obstacle.report` among them: nine `authentication` refusals in the log, while `instance.status` still said `turn` and nothing told the operator. A rule of the configuration (the lifetime is longer than `limits.turn_seconds`) only held for the first turn of a token.

Three ways were weighed: renew the token in place, give it the duration of the turn, or issue one at each turn. The decision takes the first and the last together.

- **A token per turn.** Each turn gets a token of its own when it starts, and the hub revokes it when the turn ends (`token.revoke`, `why: turn_end`), or when the turn could not start. Between two turns, no token of the instance works. This is stricter than before, when a token outlived its turn by hours: a copy that a turn left in a file, or that a later turn finds in the memory of the agent, is refused.
- **Renewed while the turn runs.** The watchdog renews the token of a turn, in a round where it has just seen the turn running, when less than `tokens.rotate_before_seconds` of its lifetime remains, and never later than four rounds of the watchdog before its end: it gets the lifetime again, counted from then (`token.renew`). The secret does not change, so the engine has nothing to read again, which it could not do (ADR 0013). A round that cannot see the turn renews nothing, and the third such round cuts the instance off by revoking its tokens, as before: a token outlives by one lifetime at most the last round that saw its turn. `tokens.lifetime_seconds` no longer has to be longer than the longest turn; a shorter lifetime is allowed, and bounds that time. Expiry (EXG-57) stays a bound: on the time the hub last saw the turn, not on the time it was issued.
- **A refused token is seen.** When the hub refuses a token it issued to an instance (revoked, expired, from another address, on another endpoint), the refusal is logged as before (`authentication`), `instance.status` says so (`token_refused`: when, why, which token, and whether it was the token of the turn under way), and the main agent is told once per token (`instance.token_refused`, events 2.2.0), unless the operator is stopping the instance. A turn whose own token is refused as expired or revoked has lost the hub: the watchdog stops it at its next round (`turn.end`, result `token_refused`) instead of leaving it `turn`. A token presented from another address ends nothing: whoever holds a copy elsewhere cannot stop the turn with it, and the operator sees the refusal.

Revocation and isolation do not weaken: the token is still bound to the address of the guard, revoked first when the instance stops, the instance is still cut off when the watchdog loses it, and a restart of the hub still revokes every token it finds. The renewal is a write on the record of a live token only: an expired or revoked token is never brought back.

### Alternatives considered

- **Only a token per turn, with the lifetime longer than the longest turn**: it keeps a turn from outliving its token as long as the watchdog ends every turn at `limits.turn_seconds`; but the lifetime then has to cover the longest turn, two hours by default, and the bound it sets, should the hub lose track of a turn without restarting, can be no shorter. With the renewal, the lifetime is chosen for that bound alone.
- **The duration of the turn as the lifetime**: the same, with no lifetime of its own to configure.
- **Renewal alone, the token kept across turns**: a token that never dies while the instance lives, against the revocation the turns give for free.
