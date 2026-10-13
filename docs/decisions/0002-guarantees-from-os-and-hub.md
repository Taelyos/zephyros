# 0002. Guarantees come from the operating system and the hub, never from prompts
- Status: Accepted
- Date: 2026-10-09

## Context
Hermes' own security policy states that the only boundary against an adversarial LLM is OS-level isolation. Its approval rules and write guards are heuristics: a terminal command such as `python -c`, `cp` or `sed -i` bypasses file-write guards. A guarantee that depends on the model obeying its instructions can therefore be defeated by prompt injection.

## Decision
Every agent runs inside a container whose whole Hermes process is confined, including its plugins, MCP servers and kanban tools. Security guarantees come from the operating system and the hub only.

## Consequences
- Positive: a manipulated agent cannot exceed what its container allows, whichever tool it invokes.
- Positive: the guarantee can be verified at the container boundary, not by reading prompts.
- Negative: anything an agent must reach on the host has to be exposed explicitly, which adds setup work.

## Alternatives considered
- Rely on Hermes approval rules and denylists.
- Use the per-tool Docker backend only, which would leave plugins, MCP and kanban on the host.
