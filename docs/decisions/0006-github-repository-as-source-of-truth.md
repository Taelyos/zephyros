# 0006. The user's GitHub repository is the source of truth; v1 stops at an accepted pull request
- Status: Accepted
- Date: 2026-10-09

## Context
Users must keep ownership of their code, including code in repositories that already exist. A Zephyros-hosted repository, or a Zephyros-run deployment, would tie users' code and releases to the project. Every change should stay reviewable in the user's own history.

## Decision
The user's GitHub repository remains the source of truth. The hub is only a staging area: the contractor pushes delivery branches there. After a PASS, the hub opens a pull request with an evidence report. The user accepts the pull request, and v1 ends there. Deployment stays with the user's own CI/CD. Existing repositories are supported from day one.

## Consequences
- Positive: code and history stay in the user's own repository.
- Positive: every pull request arrives with an evidence report for review.
- Positive: Zephyros stays out of production operations, which keeps its scope small.
- Negative: v1 cannot follow a delivery into production, since deployment stays with the user's CI/CD.

## Alternatives considered
- The hub hosts all repositories.
- Zephyros deploys to production.
