# 0028. The contract of a project is stored in SQLite, as immutable versions

- Status: Accepted
- Date: 2026-10-14
- Decided by the project owner on 2026-10-14.

## Context

The first architecture of the hub kept the contract of each project in a git repository of the hub (ADR 0003 lists a git staging area among what the hub holds): a commit per version would have given the history, the diffs and the hashes. The code took another path from the start and keeps the contract in the hub's SQLite database. This record states that choice, which the project owner accepted on 2026-10-14, and what it does and does not keep.

## Decision

The contract of a project lives in the hub's SQLite database, with the rest of its state.

- **Versions are immutable.** Table `contract_versions` holds every version of every project: its number, an identifier made of the number and the start of its content hash, the identifier of its parent, the SHA-256 of its canonical content, the whole content as JSON, the author, the time, and whether the operator wrote it. Triggers refuse any `UPDATE` or `DELETE` on it. A version is added in the same transaction that resolves the change that produced it.
- **A change waits for a decision.** Table `contract_changes` holds each proposed change: the proposer, the version it was based on, the summary and the operations, its status, and once it is decided, who decided, when, and the version that resulted.
- **The log tells the story.** Every proposal, approval, rejection and operator write is a record of the hub's log, a hash chain with an anchor outside the database (ADR 0019).
- **No git process in the hub.** The hub runs no `git` for the contract; the delivery area and the forge keep git where code is (ADR 0006).

## Consequences

- Positive: one store and one transaction for a decision and the version it makes, so a change can never be approved without its version, nor a version exist without its decision.
- Positive: fewer moving parts: no repository to initialise, lock, repair or back up apart from the database, and no subprocess in the path of every contract call.
- Positive: every version can be read back exactly (`contract.read` with a number), and its hash is in the evidence report of each delivery.
- Negative, said plainly: **not everything is history.**
  - A row of `contract_changes` is changed in place when the change is decided (`UPDATE` of its status, who decided, when, and the resulting version). Its proposal (summary, operations, base) is never rewritten, and the decision is also in the log; but the table itself keeps the last state of each change, not each state it went through, and no trigger stops another update of a decided row.
  - The acceptance tests of the Owner side (table `acceptance_tests`, one row per project and path) are **overwritten** by each `acceptance.put`. The log keeps the path and a digest of each content written, and the evidence report of each delivery names the digest of each test it ran; the earlier contents themselves are gone.
- Negative: diffs between versions are computed by whoever reads two versions, not stored, and there is no `git log` to browse.

## What comes next

Proposed, for a decision of the project owner:

1. Make `contract_changes` append-only for a decided change: a trigger that refuses any `UPDATE` other than the one from `pending` to a decision, and any `DELETE`.
2. Keep every version of an acceptance test: an append-only table keyed by project, path and content digest, with the time and the author, of which the current test is the latest row; the verification keeps reading the latest, and an evidence report can then point to the exact content it ran.

## Alternatives considered

- **A git repository per project in the hub**: history and diffs for free, but a subprocess, a working tree, locks and repairs, and a second place for state that must stay consistent with the database and the log.
- **Files on disk, one per version**: immutable by convention only, and no transaction with the decision.
