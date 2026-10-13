# 0011. Draw on BMAD, do not build on it
- Status: Accepted
- Date: 2026-10-09

## Context
BMAD offers templates for PRDs, specifications and architecture, and its Test Architect module defines gate logic. But BMAD is designed for a human driving a coding tool session by session. It does not separate the two sides by rights, and it knows tests only as a verification method.

## Decision
Zephyros reuses ideas from BMAD's PRD, specification and architecture templates and from the gate logic of its Test Architect module, adapted to two sides. We credit BMAD (MIT). "BMad" is a trademark, so we say "inspired by BMAD" and never use that name for the product.

## Consequences
- Positive: we reuse existing templates and gate logic without adopting a session-by-session workflow.
- Positive: the product name stays clear of the BMad trademark.
- Negative: we maintain our own adapted version of these ideas and do not receive BMAD's upstream changes.

## Alternatives considered
- Build Zephyros as a BMAD module.
