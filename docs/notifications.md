# Notifications

The hub tells the main agent about the transitions it must not miss. It does so over the
signed webhooks of EXG-43: one HTTP delivery per event, signed, fresh and single-use. When
`[notifications]` sets neither key, nothing is sent — the events are still recorded in the log.

The hub never waits for the receiver. An event is queued where it happens, and a thread of the
hub that does nothing else sends the queue in order. A receiver that takes its time delays only
the notifications behind it: the listeners, the verification of deliveries and the watchdog of
the instances go on.

## Events

| Event | When | Fields |
| --- | --- | --- |
| `delivery.decided` | A delivery received its gate decision (PASS, CONCERNS or FAIL), or a new one after an inspection (ADR 0020); or it was refused before any test ran (REFUSED), or its verification could not run to an end (ERROR) | `project_id`, `delivery_id`, `branch`, `decision` |
| `delivery.loop` | A streak of refused deliveries reached `continuity.rejections` without a pass in between (EXG-23) | `project_id`, `branch`, `rejections` |
| `delivery.reviewed` | The Owner side recorded its verdict on a delivery (`accept`, `accept_with_reservations` or `reject`; ADR 0020): a `reject` is the main agent's to act on | `project_id`, `delivery_id`, `branch`, `decision`, `verdict`, `reasons` |
| `continuity.alert_main` | A task stayed without activity through the wake-up and the restarts (US-E7-01); or a change the Owner side proposed awaited a decision for `continuity.inactivity_seconds` (ADR 0023) | `project_id`, `side`, `step`, `task`, `detail` |
| `continuity.alert_human` | The main agent did not unblock the task within `continuity.unblock_seconds` | `project_id`, `side`, `step`, `task`, `detail` |

## The continuity ladder

The hub keeps up to two tasks for each project, each carried by one side (`side` in the
alerts). The Contractor's task is a delivery, from the moment the base of its staging area is
ready until a delivery is declared. The other is a decision, while a change of the contract that
could still be approved awaits one. Any call of the instance that carries a task counts as
activity; the calls of the main agent do not, so its alerts come when they are due. A task
without activity for `continuity.inactivity_seconds` walks the ladder: the hub
wakes the instance with a turn that states the task, then restarts it, then sends
`continuity.alert_main`, then `continuity.alert_human`.

The decision task is about the oldest change that could still be approved
([ADR 0023](decisions/0023-a-decision-is-awaited-from-whoever-takes-it.md)). A later proposal
leaves its ladder where it is: only a decision starts it again, on the next change if one is
left. Whoever is to decide the change carries the task. For a change of the operator or the
main agent, that is the Owner side (`owner`). For a change of the Owner side, which cannot
approve its own, it is the main agent (`main-agent`): nothing is woken, the main agent is
alerted after `continuity.inactivity_seconds`, then the human after `continuity.unblock_seconds`,
whatever the Owner side does meanwhile.

The Contractor's task waits for its contract
([ADR 0021](decisions/0021-the-ladder-waits-for-a-ready-contract.md)). Its ladder starts only
once the version in force names at least one acceptance test, and while no change that could
still be approved awaits a decision. Preparing the base first, so that the Owner side can read
it, wakes no one. A change proposed while the Contractor works stops its ladder (a turn already
running goes on). Once the contract is ready again, the ladder starts over from the wake: the
task could not be carried while it was held. A version that comes while the Contractor carries
its task leaves its ladder where it is.

The log says why a task waits, so that a held task is not mistaken for a ladder that stopped. A
`continuity.step` record with the step `hold` gives the reason in `why`: `no_acceptance_test`,
or `pending_decision`. A record with the step `release` marks the start of the ladder.

The hub keeps each task, why it is held and where its ladder stands in its database. After a
restart of the hub, a held task is still held and a ladder goes on from its step. The time the
hub was down counts as time without activity: a step that fell due meanwhile is taken at the
first round of the watchdog. At its start, the hub also checks each task against the contract
as it is, in case it stopped between a change of the contract and acting upon it.

## Configuration

```toml
[notifications]
url = "https://main-agent.example.invalid/zephyros"
secret = "a-shared-secret-of-at-least-16-characters"  # pragma: allowlist secret
```

Set both keys, or neither: with neither, notifications are off. The hub refuses to start with a
`url` and no `secret` of at least 16 characters, or with a `secret` and no `url`. The secret
signs every delivery and never leaves the hub (EXG-49 masks it in logs and reports).

## What the receiver sees

- `X-Zephyros-Delivery` — the identifier of the delivery, unique per event.
- `X-Zephyros-Timestamp` — the moment of the signature, in seconds since the epoch.
- `X-Zephyros-Signature` — `HMAC-SHA256(secret, "<delivery>.<timestamp>.<body>")`, hex.

The body is JSON: `{"event": "...", "schema_version": "2.0.0", ...fields}`. `schema_version` is
the version of the event payloads, on every notification. Version 2.0.0 (Zephyros 0.2.0) is
incompatible with 1.1.0: `decision` can be `REFUSED` or `ERROR`, and `side` can be `main-agent`.
A receiver should accept values it does not know, or check `schema_version` first.

The events of the delivery loop also carry `simulation`: `true` when the hub runs with the simulated model provider (EXG-46). A
receiver verifies the signature, refuses a timestamp older than five minutes and refuses a
delivery identifier it has already seen; [docs/webhooks.md](webhooks.md) shows the reference
verifier (`zephyros.core.webhooks`).

Every notification is recorded in the log once, as `notification.sent`, so what the main agent
was told, and when, is auditable like any other action. The outcome is `ok`, or `error` with
the reason in `error`:

| `error` | What happened |
| --- | --- |
| `status <code>` | The receiver answered with a status outside 2xx; a redirect is never followed |
| `egress` | The hub refused the host before sending (EXG-48); an `egress.refused` record names it |
| the kind of failure, such as `ConnectError` | The receiver could not be reached; the message is not kept, as it may hold the url |
| `queue_full` | A thousand notifications were already waiting; this one was not queued |
| `hub_stopped` | The hub stopped before it was sent |
