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
url = "https://main.example.test/zephyros/hook"  # empty turns notifications off (default)
secret = ""  # 16+ characters, shared with the receiver out of band
```

Both keys are set together; the secret travels out of band, never through the hub's answers or the
log. The events that fire a delivery are the transitions of a project — a delivery decided, a turn
abandoned — sent by the hub as they happen.
