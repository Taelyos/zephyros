# 0008. A model gateway in the hub; instances never hold provider credentials
- Status: Accepted
- Date: 2026-10-09

## Context
Copying a subscription login into many containers breaks whenever its token refreshes, and it exposes the whole account without any spending cap. Spending must be visible and bounded per project.

## Decision
The user configures model providers once in the hub, optionally importing an existing Hermes setup. Each instance points its Hermes `base_url` at the gateway and authenticates with its project token. The hub picks the model for each role from a preset (Economy, Balanced or Maximum), counts tokens and cost per project, role and model, and enforces budgets.

## Consequences
- Positive: provider credentials exist only in the hub, never in a container.
- Positive: spending is measured and capped per project, role and model.
- Negative: the hub becomes a high-value target, since it holds all provider credentials.
- Negative: every model call passes through the hub, so its availability becomes critical.

## Alternatives considered
- Copy provider credentials into each container.
- Ask the user to configure each instance.
