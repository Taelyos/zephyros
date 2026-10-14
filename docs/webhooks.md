# Webhooks

The hub posts signed notifications to one destination — the main agent — when the operator enables
them in `[notifications]`. Each delivery is verifiable by the receiver: it can tell a call of the
hub from a forged or replayed one (EXG-43, ATK-78).

## What a delivery carries

Three headers, and a JSON body:

| Header | What it holds |
| --- | --- |
| `X-Zephyros-Delivery` | An identifier, unique per delivery; the same one is never sent twice |
| `X-Zephyros-Timestamp` | Unix seconds, the moment of the sending |
| `X-Zephyros-Signature` | HMAC-SHA256 of `id.timestamp.body` under the shared secret, hexadecimal |

## How a receiver verifies

`zephyros.core.webhooks.verify` is the reference verifier; a receiver keeps the identifiers it
accepted and refuses:

- a delivery with a missing header (`missing`);
- a wrong signature, or a body modified in flight (`bad_signature`);
- a timestamp more than five minutes away from its own clock — a valid signature of old age
  (`stale_timestamp`);
- an identifier it already accepted (`replayed`).

It logs every refusal and runs no turn for it. The identifier is added to the store only once the
delivery is accepted.

## Configuration

```toml
[notifications]
url = "https://main.example.test/zephyros/hook"  # http(s); empty turns notifications off (default)
secret = ""  # 16+ characters when url is set; empty when url is
```

Set both keys, or neither: with neither, notifications are off. The hub refuses to start with a
`url` and no `secret` of at least 16 characters, or with a `secret` and no `url`. The secret
travels out of band, never through the hub's answers or the log. The events that fire a delivery, and their payloads, are listed in
[notifications](notifications.md): a delivery decided (refused and in error included), a loop of
failed deliveries, a verdict of the Owner side, and the alerts of the continuity ladder.

The hub queues each notification and sends the queue in order from a thread of its own: a slow or
absent receiver holds up nothing else. Each notification is logged once, as `notification.sent`:
sent, or an error that says why (`status <code>`; `egress`, for a destination the hub may not
reach; the kind of error, such as `ConnectError`, for a receiver out of reach; `queue_full`; or
`hub_stopped`, for what was still queued when the hub stopped). A notification is sent once: the
hub does not retry.
