# 0027. The network guard runs without root; its one capability comes from nft

- Status: Proposed
- Date: 2026-10-14
- Revises: [0015](0015-per-instance-networks-with-a-network-guard.md) (no-new-privileges for the guard)

## Context

The network guard of an instance (ADR 0015) loads the rules of its instance's network namespace, so it needs NET_ADMIN there. It ran as user 0, with every capability dropped but NET_ADMIN and with no-new-privileges, because a user other than root holds no effective capability in a container: the runtime puts the added capabilities in the bounding set only, and the rules could not load.

Root in the guard gave it, measured, nothing more than NET_ADMIN: its effective, permitted and bounding sets were NET_ADMIN alone, its root file system is read-only, it mounts nothing, and the runtime keeps its `/proc/sys` read-only by default. But user 0 in a container is user 0 of the machine when the runtime uses no user namespace, which is the default, and several container escapes of the past needed exactly that.

The project owner asked on 2026-10-14 to try, in limited time, the standard ways of running the guard without root, and to keep root only if none works cleanly.

## What was measured

In a real container from the guard image, with `--cap-drop ALL --cap-add NET_ADMIN`, a read-only root, and the rules loaded by `nft -f`:

| User | no-new-privileges | How NET_ADMIN would reach nft | Rules load |
| --- | --- | --- | --- |
| 0 | on | effective set of root | yes (the former guard) |
| 65534 | on | none | no: `Operation not permitted` |
| 65534 | on | file capability `cap_net_admin+ep` on nft | no: no-new-privileges makes the kernel ignore file capabilities on exec |
| 65534 | off | none | no |
| 65534 | off | file capability `cap_net_admin+ep` on nft | **yes** |

Ambient capabilities are not a way: the runtime offers no option to set them, and an unprivileged user starts with empty inheritable and ambient sets (measured), so it cannot raise them.

## Decision

- The guard runs as user and group 10002.
- Its image gives the file capability `cap_net_admin+ep` to `/usr/sbin/nft` alone, owned by `root:10002` with mode 0750, so that only the guard's group (and root) can run it. `setcap` is removed from the image once it has written the capability.
- The filter starts the guard with `SecurityOpt: ["no-new-privileges:false"]`, written out so that a runtime whose default is on does not take the capability away. Every other container (turn, sandbox, hub, filter) keeps no-new-privileges. The filter's allow-list refuses a guard as root, and a guard with any other security options.

Without no-new-privileges, an exec could only gain what the bounding set allows, NET_ADMIN, and only from a program that carries it: the image holds no setuid or setgid program (the runtime test checks it), its root file system is read-only, and setting a file capability needs CAP_SETFCAP, which the bounding set does not hold. So the guard can reach NET_ADMIN only through its nft, as root could, and it no longer is user 0.

## Consequences

- Positive: the guard is no longer user 0 of the machine. Every process of the guard runs with no effective capability; NET_ADMIN exists only while nft runs (runtime test `test_adr_0027_the_guard_runs_without_root_and_only_its_nft_holds_net_admin`).
- Positive: the guard still filters: the runtime tests of the guard, the levels and the network containment pass unchanged.
- Negative: no-new-privileges is off for the guard, against the hardening profile that asks it of every container. The argument above is why that is not a loss here; it holds as long as the image gains no setuid program and no other file capability.
- Negative: the guard image depends on the runtime keeping extended attributes (`security.capability`) in image layers, which the usual storage drivers do. Where it does not, the guard cannot load its rules, is unhealthy, and no instance starts: the failure is closed.
- Neutral: the tests that need a route or an address inside the guard's namespace (a stand-in for the local network) run them as root of the guard's container, with the NET_ADMIN of its bounding set.

## Alternatives considered

- **Keep root.** The fallback had no way worked; it leaves the guard as user 0 of the machine.
- **A setuid helper.** Needs no-new-privileges off too, and a setuid program is what that option exists to stop; a file capability is narrower.
- **A user namespace for the guard** (runtime user remapping). A daemon-wide setting of the machine, outside what Zephyros configures.
