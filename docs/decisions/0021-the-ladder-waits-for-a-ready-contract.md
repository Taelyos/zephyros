# 0021. The continuity ladder waits until the contract is ready for the Contractor
- Status: Proposed
- Date: 2026-10-14

## Context
The continuity ladder (US-E7-01) resumes a task that stays without activity: it wakes the
instance, restarts it, then alerts the main agent and the human. The Contractor's task began
as soon as the base of its staging area was ready. But the base comes first: the Owner side
reads it to write the stories and the acceptance tests. The Contractor's work only begins once
that contract is settled. In the comparison run of 2026-10-13, the hub woke the Contractor
900 s after the base was ready each time the Owner side wrote for longer than that. Two
attempts were abandoned after the Contractor spent whole turns on a draft ($2.46), and an
external guard had to stop such turns from then on.

## Decision
- The Contractor's task still begins when the base is ready, and a declared delivery still
  answers it. While the contract is not ready for the Contractor, the task is **held**: it walks
  no ladder.
- The contract is ready for the Contractor when the version in force names at least one
  acceptance test (the criterion of a story that covers a requirement or a constraint), and no
  change that could still be approved awaits a decision. A change proposed on an older version
  can no longer be approved, so it does not count.
- The hub checks again each time a version or a pending change comes or goes
  (`contract.propose`, `contract.approve`, `contract.reject`, `contract.apply_reserved`,
  `contract.import`). A released task starts its ladder from the wake, counted from the
  release. A version that comes while the task is not held leaves its ladder where it is. A turn
  already running is not stopped.
- The log records each hold, with its reason (`why`: `no_acceptance_test` or
  `pending_decision`), and each release, as `continuity.step` records with the steps `hold`
  and `release`. The operator can tell a task held by its contract from a ladder that stopped.
- The Owner side's decision task is unchanged.

## Consequences
- Positive: the hub no longer spends a Contractor turn on a contract still being written,
  whatever order the operator follows.
- Positive: while a change awaits a decision, only the Owner side's decision task escalates.
- Negative: for a contract that names no acceptance test, the Contractor's ladder does not start
  at all: no wake, no restart, no alert, even when the operator started the Contractor by hand
  and it stalls. That covers a contract verified by inspection alone, and one imported from
  Markdown, whose criteria name no test. The log shows the task held, with its reason.
- Negative: one named test is enough. When the Owner side writes the contract in several
  proposals, approved more than `continuity.inactivity_seconds` apart, the Contractor can be
  woken between two of them.
- Negative: every proposal resets the Owner side's decision task and holds the Contractor's. An
  Owner side that proposes more often than `continuity.inactivity_seconds` (a model in a loop,
  or a hostile instance) keeps both ladders silent. A later ticket on the decision task covers
  this.
- Negative: the delivery loop's thread reads the contract, then creates the task. An approval
  between the two releases a task that does not exist yet, which stays held until the next
  change of the contract. The window is a few milliseconds; a lock is planned (T12).
- Negative: the ladder still lives in the hub's memory only. After a restart of the hub, the
  Contractor's task begins again with the next preparation of its base.
- Negative: the Owner side's decision task still ends with any decision of the project. If
  another change that could still be approved remains, the Contractor's task stays held and no
  ladder runs until that change is decided (`contract.pending` lists it).

## Alternatives considered
- Begin the task at the approval of a change rather than at the preparation of the base. An
  operator who imports a whole contract approves nothing, and a later approval would wake a
  Contractor that has already delivered.
- Check readiness on each beat of the watchdog. The instance manager would have to read the
  contract, and the ladder would count the time before the contract was ready.
- Count a story that covers a requirement, rather than a named test. The Markdown importer
  writes stories and criteria without tests, so the Contractor would be woken while the Owner
  side still writes the acceptance tests.
- Require a test for every requirement verified by test. This is stricter: the ladder would stay
  held on a partly covered contract, which the gate judges as it is anyway.
