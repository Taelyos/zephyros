# 0026. A lifetime and a rotation for the operator and main-agent tokens

- Status: Accepted
- Date: 2026-10-14
- Decided by the project owner on 2026-10-14.
- Refines: [0013](0013-opaque-server-side-tokens.md)

## Context

ADR 0013 gave every token a server-side record with an expiry, and project tokens expire (EXG-57). The operator token and the main-agent token did not: they were issued once, at the first start of the hub, never expired, were bound to no address, and could only be replaced by deleting the file and restarting the hub. Yet they hold every right of their channel: the operator writes the reserved data of every project, the main agent creates projects and speaks for the human. A copy of either, left in a backup, a shell history or the memory of an agent, stayed useful forever.

The project owner decided on 2026-10-14 that both expire after a configurable lifetime, are refused clearly once expired, and can be rotated by a documented command, without breaking a deployment that is updated.

## Decision

- **A lifetime.** `tokens.channel_lifetime_days` (1 to 3650, default 90) sets the lifetime of both tokens. The hub issues them with it.
- **No silent break.** At each start, the hub gives a token of either kind that has no expiry, and is not revoked, the configured lifetime counted from that start, and logs it (`token.lifetime`). A deployment updated today keeps its tokens working for the whole lifetime, and they then expire like the others.
- **A file that holds no working token** (missing, unknown, revoked, expired) gets a new token at the start of the hub, as before, now with the lifetime, and the hub logs why (`token.issue`, `why`).
- **A rotation.** `scripts/deploy.sh rotate-token operator|main-agent` runs `zephyros hub rotate-token` in the hub container. The running hub issues a new token, writes it to the token file (replaced whole, mode 0600), then revokes every other live token of that kind, and logs the issue and each revocation (`token.issue` and `token.revoke`, `why: rotation`). If the file cannot be written, the new token is revoked and the old one stays. The answer names the file and the new expiry, never the token. Turns and instances are not touched.
- **The control socket.** The command reaches the hub on a Unix socket (`paths.control_socket`, in the data directory, mode 0600, the user of the hub alone). Reaching it is the authentication: whoever can open it can already read the token files. It must work once the operator token has expired, so it cannot ask for that token; and it must be the running hub that rotates, because the log is a chain with an anchor outside the store, which a second writer would break.
- **A clear refusal.** An expired operator or main-agent token is answered `401` with `{"error": "token_expired", "message": …}`: when it expired, and the command that gives a new one. Every other refusal still says nothing about why. The holder learns nothing it did not know: it presented that very token. The refusal is logged as every refusal is (`authentication`, reason `expired`).

### Why 90 days

The lifetime bounds how long a copy that leaked stays useful; its cost is a rotation by hand, and for the main-agent token, giving the main agent the new token. A week or a month would make that chore frequent enough to be put off, or worked around with a very long value; a year would leave a leaked copy useful for most of a year. A quarter keeps a leaked copy useful for at most three months, and asks the human for four rotations a year, which the clear refusal and the one command keep short. `scripts/deploy.sh call` reads the operator token from its file at each call, so its rotation costs the operator nothing. An operator who wants less sets fewer days.

## Consequences

- Positive: a copy of either token stops working at the latest one lifetime after it was issued, and at once after a rotation.
- Positive: an expired token says what to do; the operator never needs to restart the hub, which would stop the turns under way.
- Negative: the main agent stops working when its token expires until the human rotates it and gives it the new one. The refusal says so; the hub does not warn ahead of the expiry.
- Negative: one more listener, the control socket, local to the hub container.
- Neutral: the tokens stay bound to no address. The operator and main-agent endpoints are reached through the publication of their ports (ADR 0025), whose peer is the runtime's, not the caller's: a binding would bind to the runtime.

## Alternatives considered

- **Rotation through the operator channel**: needs a live operator token, so an expired one could never be replaced without a restart.
- **A command that writes the database directly**: a second process appending to the log would break its chain and its anchor (ADR 0019).
- **Renewing the tokens automatically before they expire**: the main agent holds a copy, which would stop working without the human knowing why or when.
- **Restart the hub to rotate**: it already renews a token that no longer works, but a restart stops every turn under way.
