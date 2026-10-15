# 0030. The tools of a project are mounted read-only, on both sides and at the gate

- Status: Accepted
- Date: 2026-10-15
- Decided by the project owner on 2026-10-15 for the direction: heavy, fixed tooling that no side fetches or changes, the same for the agents and for the gate. The design below is the coordinating agent's.
- Extends: [0004](0004-sandboxed-execution-of-delivered-code.md), [0017](0017-verification-runner-and-kits.md) and [0024](0024-three-levels-of-network-access.md)

## Context

Some projects need tooling that is heavy and fixed: a simulator, a headless engine, a test bench and its measurement scripts. The deliverable of such a project is judged by running it in that tooling, for instance a program evaluated in a simulator of a few hundred megabytes with Python scripts that score a run.

Nothing in Zephyros carried such tooling. A `sealed` instance installs nothing ([ADR 0024](0024-three-levels-of-network-access.md)), a `regulated` one only from the allowed registries, and the image of the `tests` kit holds Python and pytest alone ([ADR 0017](0017-verification-runner-and-kits.md)): the acceptance tests of the Owner side could not start the simulator at the gate. Had each side fetched the tooling itself, nothing would have shown that the gate judged with the tooling the agents built and tested against, nor kept an agent from changing it.

## Decision

**One directory of the machine, named by the operator, is mounted read-only at `/opt/project-tools` in every turn, Owner and Contractor, and in every sandbox of verification.** The configuration names it (`tools.source`, its path as the container runtime sees it); `scripts/deploy.sh` takes it from `ZEPHYROS_TOOLS`. The path inside the containers is fixed, so that an acceptance test, a script or a note of the contract that names it holds on both sides and at the gate, and `/opt/project-tools/bin` comes last on the `PATH` of the three images (Hermes, OpenCode, the `tests` kit): a tool there runs by its name and never hides a program of the image. The Hermes and OpenCode images also add it from `/etc/profile.d`, because the terminal of Hermes runs its commands in a login shell, whose `PATH` `/etc/profile` sets again.

**One directory for the deployment, not one per project.** The configuration of the hub has no table per project: the projects are created at run time, with identifiers the hub issues, and every other choice of this kind (the network level, the engine of the Contractor) is one for the deployment. A choice per project, among directories the configuration names, can come later without changing the mount, the checks or the fingerprint.

**The mount.** The filter builds it from its template, the one bind of a host path it makes: read-only, not recursive (a file system mounted somewhere under the directory on the machine is not carried in, so it can neither escape the read-only flag nor the fingerprint) and with private propagation. `check_spec` refuses any other bind, and, when the configuration names tools, a turn or a sandbox without exactly this one: the gate cannot run with other tools than the turns, nor a turn without them.

**What the hub refuses.** The hub reads the directory at the same path (`deploy/compose.yaml` mounts it into the hub container there, read-only) and refuses to start, naming each path at fault, when:

- the user of the instances (uid and gid 10000) could change an entry: it owns one, its group may write one, every user may write one, or an entry carries an access control list (whose grants this check does not read);
- a link is absolute (inside a container it would point at something else) or leads out of the directory;
- an entry is neither a directory, a regular file nor a link (a device, a socket, a pipe), a file is setuid or setgid, or a file cannot be read by the hub (it could not be fingerprinted).

`scripts/deploy.sh` runs the same check (`zephyros tools check`), as the user of the hub, before anything starts. The check is made again at each start of a turn and at each verification: a turn whose tools have become refused does not start (`turn.start`, `error: tools_refused`, with the paths), and such a verification ends in an error.

**The fingerprint, so that a verdict can be replayed.** The SHA-256 of one JSON line per entry, sorted by path: a directory, a file with its execute bit, its size and the SHA-256 of its content, a link with its target. Owners, times and the other mode bits do not count, so the same tree copied to another machine has the same fingerprint, and `zephyros tools check DIR` computes it there. The hub records it:

- when it starts, in the log of the hub (`tools.check`, with the path, the number of files and their size);
- at each start of a turn, in the log of the project (`turn.start`: `tools`);
- before and after each verification (`verification.run`: `tools`). A change between the two drops the results: the verification ends in an error and decides nothing;
- in the evidence report: the fingerprint, the number of files and their size; and, as a flag, the turns of either side before the delivery that found other tools, or none.

Hashing a few hundred megabytes takes a second or two; the hub keeps the result with the status of every entry (size, times, inode, mode, owner) and hashes again only when one of them changed. A change of content changes the change time (ctime) of the file, which no program sets at will.

**The duration of a verification.** `verification.timeout_seconds` goes up to 86 400 s (it stopped at 3 600 s), the bound of a turn: an acceptance test may play several runs of a few minutes each. The default stays 300 s.

## What it guarantees, and what it does not

It guarantees that:

- nobody inside Zephyros can change the tools: neither side, nor the delivered code at the gate, nor a later turn. The mount is read-only, the files are not the instances' user's, and the filter builds no other bind;
- the gate judges with the tools the turns had: the same directory, at the same path, in the same mount, and the report says when a turn found other tools;
- a verdict names the bytes it was reached with: the fingerprint is in the log and in the report, and the tools can be checked against it anywhere.

It does not guarantee, because the tools are the operator's and trusted:

- what the tools do. Zephyros runs them where it runs delivered code: no network in the sandbox, the limits of a turn, user 10000. It does not judge whether a simulator scores fairly, nor keep a tool from reading what the delivery left in `/work`;
- that the tools stay the same on the machine. The operator, or anyone who may write the directory on the machine, can change it between two runs: the fingerprint shows it, nothing prevents it;
- that nobody reads them. Both sides read the tools: what the Contractor must not see (the hidden part of an evaluation) belongs to the acceptance tests of the Owner side, never to the tools. The tools hold no secret;
- that a tool runs in every image. The three images do not share a C library: the Hermes image is Debian 13 (glibc 2.41), the OpenCode image and the `tests` kit are Alpine 3.24 (musl). A program linked against the glibc of the machine runs in a Hermes turn and fails at the gate ("not found"). The tools must carry what they run on: statically linked programs, or programs started through the dynamic loader and the libraries the directory holds ([docs/configuration.md](../configuration.md), "The tools of a project"). Likewise `python3` is 3.14 in a Hermes turn and 3.12 at the gate.

## Consequences

- Positive: a project whose deliverable is judged in a simulator can run in Zephyros, `sealed` included, with the simulator fixed for both sides and for the gate, and its verdict tied to the simulator's bytes.
- Positive: the runtime tests mount a small tools directory (a static program, a program started through the glibc loader it carries, and a script) in an Owner turn, a Contractor turn of Hermes and of OpenCode, and the sandbox, show it visible, runnable and impossible to change, and show a delivery whose acceptance test calls it pass the gate at the `sealed` level (`tests/runtime/test_project_tools.py`).
- Negative: the delivery loop runs one verification at a time, for every project. A verification of two hours makes the next deliveries of every project wait for it.
- Negative: the sandbox has the limits of a turn (`limits.cpus`, `limits.memory_mib`, `limits.pids`) and a `/tmp` of 64 MiB where nothing runs. A heavier tool means higher limits for the turns too, and a tool that unpacks programs to run them must unpack them in `/work`.
- Negative: on macOS, the owner the runtime shows for a directory shared from the host may not be the owner on the host; the check judges the owner the hub sees. It has been run on Linux only.
- Negative: the fingerprint is computed by walking the whole tree, at each start of the hub and of a turn, and twice for each verification: a tree of many thousands of files costs that walk each time, its hash only when it changed.

## Alternatives considered

- **The tools in the images** (a derived image per project): the gate's image and the turns' would need building per project and could drift apart; the fingerprint of three images says less than one of the tools.
- **The tools fetched by each side** (an allowed host, a release download): the level `sealed` would not allow it, each side could fetch something else, and an agent could change what it fetched.
- **A Docker volume filled by the operator**: the same mount, with one more copy to keep in step, and a volume is not something the operator edits in place; a directory is.
- **A configurable path inside the containers**: one more key, and a test or a contract that names the path would hold only for one deployment.
- **The tools on the `PATH` first**: a tool could then hide `python` or `pytest`, in a turn and at the gate.
- **One directory per project now**: the operator would name a project identifier the hub issues at run time, then restart the hub; a named choice at `project.create` is the better form, left for when a deployment runs projects with different tools.
- **A fingerprint at the start of the hub only**: a change on the machine while the hub runs would go unseen, and the report would name tools the gate did not run with.
