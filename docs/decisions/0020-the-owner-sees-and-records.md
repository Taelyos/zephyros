# 0020. The Owner side sees what it judges, and its verdict is recorded

- Status: Accepted (implemented in C36 to C43); the amendment below is Proposed
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
- **The gate counts every requirement and every constraint, by its methods.** A requirement or constraint verified by test is evidenced by its criteria's tests, as today. One verified by inspection, analysis or demonstration is evidenced by an **inspection record**: the new tool `inspection.record` (operator, main agent, Owner instance) records, for one delivery, the object, the verdict (`conforms` or `does not conform`) and the observation. An object whose only shortfall is a record not made yet *awaits* it: it is a reservation, so the decision is **CONCERNS** at best, never PASS; FAIL when the rules fail even counting the awaited objects as verified. A record that finds the object not conforming leaves it unverified. Each record decides again from the same results; the report gains an addendum with every record and the decision now, and the log and the main agent (`delivery.decided`) hear of a new decision.
- **The Owner reads the delivery.** With its project token, the Owner instance reaches `GET /staging/base` (the client's repository) and the new `GET /staging/delivery/<id>`, which returns the delivered branch as a git bundle, like the base: the Owner reads the tree and computes the diff with git. Both are read-only and scoped to the Owner's own project.
- **Changes to protected paths are flagged.** The configuration gains `verification.protected_paths` (default: `CONTRACT.md`). A delivery that changes a protected path is flagged in the report and in the pull request. It is not refused: the hub flags, a principal decides (as for forbidden terms, EXG-29).
- **The Owner's verdict is recorded.** A new tool `delivery.review` (Owner instance, operator, main agent) records, for a delivery that passed the gate, a verdict (`accept`, `accept_with_reservations` or `reject`) and the reasons by requirement. The verdict is logged, shown by `delivery.status` and posted on the pull request as a comment (an inspection is posted there too); the main agent hears of it through the `delivery.reviewed` notification, so a `reject` is its to act on (continuity, US-E7-01). It does not merge anything: merging stays a human act. The forge interface gains `comment` (0.3.0); the delivery worker posts, never a request.
- **Resource limits follow the measurement.** The default memory limit of a turn rises to 1.5 GiB (`limits.memory_mib = 1536`), and the default turn duration to two hours (`limits.turn_seconds = 7200`).
- The 4 000-character prompt stays. The Owner now reads the repository itself, and `docs/real-test.md` tells the operator to keep the client's interface in the contract (a constraint) when the Owner must test it.

## Consequences

- Positive: the approver reads what it approves; the evidence report is honest about what tests cannot show; the Owner judges the delivered code and its effect on the client's files, not only a table of green tests; the Owner's verdict joins the evidence trail and the pull request.
- Negative: new tools and a new staging route widen the Owner's surface. Each is read-only or records data, is scoped to the Owner's own project and is logged; ATK attacks for them join the catalogue. CONCERNS will appear on deliveries that would have passed, until each project records its inspections: this is the intended effect.
- The schemas move: configuration 1.12.0 (`verification.protected_paths`, new defaults), contract unchanged, events 1.1.0 (`delivery.reviewed`); the forge interface 0.3.0.
- Attacks ATK-90 to ATK-93 (added to the threat model with this decision) test the new surface: the Contractor never judges its own delivery, a rewritten `CONTRACT.md` changes no decision and is flagged, a P0 constraint without its inspection never passes, an Owner of another project neither reads nor judges.
- `scripts/deploy.sh down` now removes the guards, turns and sandboxes the filter created outside compose (the second run left four guards), and keeps the instances' volumes.
- To decide later (Z5): whether a `reject` should also forbid opening the pull request, and whether the Contractor may answer a review (change requests between sides).

## Amendment (Proposed, 2026-10-14): never PASS without a test that ran

The decision above lets a recorded inspection verify the object it judges. It leaves open whether
a delivery that the hub never ran a test on can reach PASS. The default chosen, which the
program lead confirms or reverses, is that it cannot:

- **PASS needs evidence that the hub produced itself:** at least one acceptance test of the
  judged objects ran and passed.
- **Nothing to judge.** A contract with no requirement or constraint (an empty one, or a delivery
  declared before the contract) holds the reservation "nothing was judged".
- **No test passed.** A delivery where no acceptance test passed holds "nothing was judged by test"
  when none ran, and "no acceptance test passed" when some ran and none passed. This includes a
  contract judged by inspection alone, every inspection conforming.
- **Effect.** Either delivery is CONCERNS at best, and its pull request still opens. A test that
  ran and failed never does better than one that did not run.

Consequences:

- Positive: the hub never says PASS on judgments alone, its own or the Owner side's, nor on a
  contract that asks nothing.
- Negative: a project judged only by inspection, analysis or demonstration never sees PASS. Its
  pull requests carry the reservation, and the human merges on the inspections recorded.
- Reversible: the rule is one function of the gate (`_vacuous` in `core/gate.py`). Letting
  conforming inspections stand in for a test means returning no reservation when every judged
  object is verified. `tests/unit/core/test_gate.py` names the cases that would change.

