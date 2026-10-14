# 0013. Tokens are opaque, bound to their instance and checked against server-side records
- Status: Accepted; partly superseded by [0016](0016-one-agent-container-per-turn.md)
- Date: 2026-10-09
- Superseded in part: "Rotation by restart" below. A token is bound to the address of the
  instance's network guard and lives as long as the instance; it is revoked when the instance
  stops, and the next turn starts with a new token when the current one nears its expiry
  ([0016](0016-one-agent-container-per-turn.md)). There is no long-running container to replace.
  The rest of this decision stands.

## Context
Every instance and the main agent authenticate to the hub with a token. The threat model asks that identity be derived only from a server-side record and never from a request field (T-02), and the catalog attacks editing claims, `alg=none`, unsigned tokens, added headers and replay after the instance stops (ATK-12). A token read from a file or a tool output must be useless to another context (EXG-60, T-31, ATK-85). Tokens must also be revocable when an instance stops (EXG-42) and expire (EXG-57).

This refines [0003](0003-owner-and-contractor-instances-with-hub.md): the token no longer only *carries* the side and the project, it *identifies* an instance, which is the only context allowed to present it.

## Decision
A token is 256 random bits with a visible kind prefix. The hub stores only a hash of it, with a record: principal (a stable identifier that survives restarts), side, project, instance, bound source address, issue time, expiry and revoked flag. Each request is authenticated by looking up the hash. Nothing inside the token is read as a claim. Time comes from the hub clock only. A token never appears in a log record or an error; records name it by an identifier.

**Source binding (EXG-60).** The hub compares the TCP peer address of the connection with the registered address of the instance the token was issued to. It never uses a forwarded header or any field of the request. It refuses on doubt: no peer address, a different address, a listener that sits behind a proxy. The comparison is constant-time. The main-agent token is accepted only on the main-agent endpoint.

**Rotation by restart.** A running instance cannot be assumed to re-read its token. Before the lifetime of a token ends, the hub starts a replacement container with a new token, which is valid before the old token is revoked, then stops the old container. This keeps an instance calling the hub across several lifetimes (ATK-87). If the pinned Hermes proves able to re-read a token file, this decision is revisited.

## Consequences
- Positive: no claim parsing and no signature algorithm to attack, so edited claims, `alg=none` and added headers have nothing to act on.
- Positive: a copied token is useless from any other context, and revocation and expiry take effect on the next request.
- Positive: a database leak does not yield usable tokens.
- Negative: a lookup per request, and the token store becomes part of the trusted base.
- Negative: binding needs the hub to see a distinct, untranslated source address for each instance on every supported runtime. Spike Z2-02 must show it.
- Negative: rotation costs a restart of the instance.

## Alternatives considered
- Self-contained signed tokens (JWT or similar): revocation needs a deny-list, parsers are an attack surface, and binding is no easier.
- Macaroons: attenuation is not needed in version 1.
- Rotating the token in place: depends on an engine behavior that is not established.
