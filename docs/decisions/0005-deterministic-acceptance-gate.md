# 0005. Judge each requirement, then decide acceptance by deterministic rules
- Status: Accepted
- Date: 2026-10-09

## Context
Acceptance must be reproducible and immune to text the contractor injects. An LLM that reads all the evidence and decides meets neither requirement.

## Decision
Each requirement is judged by the method that suits it. Tests are judged by the hub. Demonstrations, inspections and analyses are judged on the owner side, where an LLM may judge. The contractor never issues a verdict. The gate (PASS, CONCERNS or FAIL) is computed by deterministic, configurable rules over recorded results. WAIVED is possible only with a signed human approval. Default thresholds, inspired by BMAD Test Architect: all P0 criteria pass, at least 90% of P1 and at least 80% overall. An agent may add concerns or fail a delivery, but never upgrade one.

## Consequences
- Positive: the same recorded results always produce the same gate outcome.
- Positive: injected text in the contractor's output cannot change the outcome.
- Negative: judgment-based requirements still depend on an LLM on the owner side.
- Negative: thresholds need per-project tuning; poor values block good work or pass weak work.

## Alternatives considered
- An LLM reads all the evidence and decides.
