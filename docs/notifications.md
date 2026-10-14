# Notifications

The hub tells the main agent about the transitions it must not miss. It does so over the
signed webhooks of EXG-43: one HTTP delivery per event, signed, fresh and single-use. When
`[notifications]` is empty, nothing is sent — the events are still recorded in the log.

## Events

| Event | When | Fields |
| --- | --- | --- |
| `delivery.decided` | A delivery received its gate decision (PASS, CONCERNS, FAIL or ERROR), or a new one after an inspection (ADR 0020) | `project_id`, `delivery_id`, `branch`, `decision` |
| `delivery.loop` | A streak of refused deliveries reached `continuity.rejections` without a pass in between (EXG-23) | `project_id`, `branch`, `rejections` |
| `delivery.reviewed` | The Owner side recorded its verdict on a delivery (`accept`, `accept_with_reservations` or `reject`; ADR 0020): a `reject` is the main agent's to act on | `project_id`, `delivery_id`, `branch`, `decision`, `verdict`, `reasons` |
| `continuity.alert_main` | A task stayed without activity through the wake-up and the restarts (US-E7-01) | `project_id`, `side`, `step`, `task`, `detail` |
| `continuity.alert_human` | The main agent did not unblock the task within `continuity.unblock_seconds` | `project_id`, `side`, `step`, `task`, `detail` |

## The continuity ladder

The hub keeps one task for each side of a project. The Owner side's task is a decision, from the
moment a change of the contract is proposed until a change is approved or rejected. The
Contractor's task is a delivery, from the moment the base of its staging area is ready until a
delivery is declared. Any call from the side counts as activity. A task without activity for
`continuity.inactivity_seconds` walks the ladder: the hub wakes the instance with a turn that
states the task, then restarts it, then sends `continuity.alert_main`, then
`continuity.alert_human`.

The Contractor's task waits for its contract
([ADR 0021](decisions/0021-the-ladder-waits-for-a-ready-contract.md)). Its ladder starts only
once the version in force names at least one acceptance test, and while no change that could
still be approved awaits a decision. Preparing the base first, so that the Owner side can read
it, wakes no one. A change proposed while the Contractor works stops its ladder (a turn already
running goes on). Once the contract is ready again, the ladder starts over from the wake.

The log says why a task waits, so that a held task is not mistaken for a ladder that stopped. A
`continuity.step` record with the step `hold` gives the reason in `why`: `no_acceptance_test`,
or `pending_decision`. A record with the step `release` marks the start of the ladder.

The ladder lives in the hub's memory. After a restart of the hub, a task begins again with the
next preparation of a base or the next proposed change.

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
