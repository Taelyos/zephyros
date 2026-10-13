# 0020. The Owner side sees what it judges, and its verdict is recorded

- Status: Proposed
- Date: 2026-10-13

## Context

The second real run put a real Owner instance and a real Contractor instance, both on a real model, on two projects. Three deliveries passed the gate, and the Owner reviewed each one. The run worked, and it showed where the Owner side is blind or mute:

1. **An approver cannot read what it approves.** `contract.propose` creates a pending change, and `contract.approve` turns it into a version. No tool shows a pending change: `contract.read` reads versions only. The operator had to open the hub's database to read the Owner's proposals before approving them.
2. **The gate ignores constraints and every method but tests.** CTR-01 ("only the Python standard library", P0, verified by inspection) had no evidence in any delivery, and the gate said PASS each time. All three verdicts of the Owner called it out.
3. **The Owner cannot see the delivery.** It reads the contract and the evidence report, never the delivered tree or its diff. It could not notice that a Contractor had rewritten the client's `CONTRACT.md` in its pull request.
4. **The Owner's verdict is not recorded.** "Accept with reservations" and its reasons existed only as text in a turn. The hub's log, the delivery and the pull request do not carry it.
5. **The Owner cannot read the repository** (its tools do not reach the staging area), and a first prompt holds 4 000 characters. The client's interface had to be moved into the contract (a constraint) for the Owner to write tests against it.

Two resource limits were also reached: a real Contractor turn peaked at 1 019 MiB against a limit of 1 GiB per turn, and the Owner's most careful turn took 41 minutes of a 60-minute limit.

## Decision

- **Pending changes are readable.** A new tool `contract.pending` lists the pending changes of a project, each with its proposer, base version, summary and operations in full. Whoever may approve or reject a change may read it: the operator, the main agent and the Owner instance of the project. The Contractor may not, since it cannot propose either.
- **The gate counts every requirement and every constraint, by its methods.** A requirement or constraint verified by test is evidenced by its criteria's tests, as today. One verified by inspection, analysis or demonstration is evidenced by an **inspection record**: the new tool `inspection.record` (operator, main agent, Owner instance) records, for one delivery, the object, the verdict (`conforms` or `does not conform`) and the observation. Without a conforming record, a P0 object makes the decision **CONCERNS**, never PASS. A P1 object counts against the 90 % rule like an unverified requirement. The report lists the records in a new section, beside the test outcomes.
- **The Owner reads the delivery.** With its project token, the Owner instance reaches `GET /staging/base` (the client's repository) and the new `GET /staging/delivery/<id>`, which returns the delivered tree and its diff against the base as a tar stream. Both are read-only and bounded by the same size limits as a push.
- **Changes to protected paths are flagged.** The configuration gains `verification.protected_paths` (default: `CONTRACT.md`). A delivery that changes a protected path is flagged in the report and in the pull request. It is not refused: the hub flags, a principal decides (as for forbidden terms, EXG-29).
- **The Owner's verdict is recorded.** A new tool `delivery.review` (Owner instance, operator, main agent) records, for a delivery that passed the gate, a verdict (`accept`, `accept_with_reservations` or `reject`) and the reasons by requirement. The verdict is logged, shown by `delivery.status` and posted on the pull request as a comment. It does not merge anything: merging stays a human act. A `reject` sends the reasons to the main agent (continuity, US-E7-01).
- **Resource limits follow the measurement.** The default memory limit of a turn rises to 1.5 GiB (`limits.memory_mib = 1536`), and the default turn duration to two hours (`limits.turn_seconds = 7200`).
- The 4 000-character prompt stays. The Owner now reads the repository itself, and `docs/real-test.md` tells the operator to keep the client's interface in the contract (a constraint) when the Owner must test it.

## Consequences

- Positive: the approver reads what it approves; the evidence report is honest about what tests cannot show; the Owner judges the delivered code and its effect on the client's files, not only a table of green tests; the Owner's verdict joins the evidence trail and the pull request.
- Negative: new tools and a new staging route widen the Owner's surface. Each is read-only or records data, is scoped to the Owner's own project and is logged; ATK attacks for them join the catalogue. CONCERNS will appear on deliveries that would have passed, until each project records its inspections: this is the intended effect.
- The schemas move: configuration 1.12.0 (`verification.protected_paths`, new defaults), contract unchanged, events 1.1.0 (`delivery.reviewed`).
- To decide later (Z5): whether a `reject` should also forbid opening the pull request, and whether the Contractor may answer a review (change requests between sides).
