# Notifications

The hub tells the main agent about the transitions it must not miss. It does so over the
signed webhooks of EXG-43: one HTTP delivery per event, signed, fresh and single-use. When
`[notifications]` is empty, nothing is sent — the events are still recorded in the log.

## Events

| Event | When | Fields |
| --- | --- | --- |
| `delivery.decided` | A delivery received its gate decision (PASS, FAIL or ERROR) | `project_id`, `delivery_id`, `branch`, `decision` |
| `delivery.loop` | A streak of refused deliveries reached `continuity.rejections` without a pass in between (EXG-23) | `project_id`, `branch`, `rejections` |
| `continuity.alert_main` | A task stayed without activity through the wake-up and the restarts (US-E7-01) | `project_id`, `side`, `step`, `task`, `detail` |
| `continuity.alert_human` | The main agent did not unblock the task within `continuity.unblock_seconds` | `project_id`, `side`, `step`, `task`, `detail` |

## Configuration

```toml
[notifications]
url = "https://main-agent.example.invalid/zephyros"
secret = "a-shared-secret-of-at-least-16-characters"  # pragma: allowlist secret
```

Both keys are set together; either empty turns notifications off. The secret signs every
delivery and never leaves the hub (EXG-49 masks it in logs and reports).

## What the receiver sees

- `X-Zephyros-Delivery` — the identifier of the delivery, unique per event.
- `X-Zephyros-Timestamp` — the moment of the signature, in seconds since the epoch.
- `X-Zephyros-Signature` — `HMAC-SHA256(secret, "<delivery>.<timestamp>.<body>")`, hex.

The body is JSON: `{"event": "...", ...fields}`. A receiver verifies the signature, refuses
a timestamp older than five minutes and refuses a delivery identifier it has already seen;
[docs/webhooks.md](webhooks.md) shows the reference verifier (`zephyros.core.webhooks`).

Every notification is recorded in the log as `notification.sent` (outcome `ok` or `error`),
so what the main agent was told, and when, is auditable like any other action.
