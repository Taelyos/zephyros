# 0004. Delivered code runs only in a disposable sandbox launched by the hub
- Status: Accepted
- Date: 2026-10-09

## Context
Delivered code can be booby-trapped. Running it inside the owner instance would expose the contract's write rights to that code, and trusting the contractor's own test reports would let the contractor grade its own work. Verification must therefore be run by the hub, outside any agent's trust boundary.

## Decision
Delivered code and its tests run only in a disposable sandbox launched by the hub. The sandbox receives no secrets, and the contract is mounted read-only. The hub, not the contractor, records the results. Each sandbox is discarded after its run.

## Consequences
- Positive: booby-trapped code cannot reach contract write rights or credentials.
- Positive: recorded results come from the hub, so the contractor cannot falsify them.
- Positive: each run's side effects stay inside its sandbox.
- Negative: every verification run depends on the hub's sandbox launcher, which adds setup and run time.

## Alternatives considered
- Run tests inside the owner instance.
- Trust results reported by the contractor.
