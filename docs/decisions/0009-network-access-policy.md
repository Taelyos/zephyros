# 0009. Network access policy for instances and sandboxes
- Status: Superseded by [0024](0024-three-levels-of-network-access.md)
- Date: 2026-10-09

## Context
Instances need Internet access to do their work. Owner instances read hostile content too, and delivered code runs in sandboxes. Services on private networks are often weakly protected, and web pages can carry prompt injection that tries to reach them.

## Decision
Every instance, Owner or Contractor, has Internet access but no route into the user's private networks. Every sandbox has no network at all by default, and no route into private networks if one is ever allowed. No egress proxy is mandatory. These destinations are blocked for every execution context: RFC 1918 ranges, loopback, link-local, IPv6 unique local and link-local addresses, the shared address space 100.64.0.0/10, and cloud metadata endpoints.

## Consequences
- Positive: a manipulated instance cannot scan or reach services on the user's private networks.
- Positive: delivered code cannot call out from a sandbox unless the configuration allows it.
- Positive: residual damage is already limited by the containers.
- Negative: instances cannot use services on private networks at all.
- Negative: there is no central egress log by default; a proxy may return later as an option for logging or for the strict level of the strictness setting.

## Alternatives considered
- An allow-list proxy for all instance traffic.
- Applying the policy to the Contractor only.
