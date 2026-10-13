# 0007. High-risk approvals are signed by the human with a passkey
- Status: Accepted
- Date: 2026-10-09

## Context
Some approvals are too consequential for an agent, or for a chat message, to grant. If an agent could produce an approval itself, it could authorize its own risky actions.

## Decision
For high-risk approvals, the hub creates an approval request that carries a hash of exactly what is approved. The human signs it with a passkey (WebAuthn). The resulting token is bound to that request, single-use and time-limited. An agent may carry the token but cannot forge it. Low-risk approvals may be "declared" by the main agent. Declared approvals are recorded as such and notified to the human, who can revoke them.

## Consequences
- Positive: the hash binds the signature to the exact content the human reviewed.
- Positive: single-use, time-limited tokens cannot be reused.
- Negative: high-risk approvals need the human at a passkey device, which slows them down.
- Negative: a declared low-risk approval stays in force until the human notices and revokes it.

## Alternatives considered
- Approvals through chat messages only.
- Dashboard clicks for everything.
