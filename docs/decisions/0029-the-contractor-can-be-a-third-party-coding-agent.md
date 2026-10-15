# 0029. The Contractor can be a third-party coding agent

- Status: Accepted
- Date: 2026-10-15
- Decided by the project owner on 2026-10-15 for the direction: any coding agent can be the Contractor, and Zephyros is the organisation and the judge around it. The design below is the coordinating agent's.
- Extends: [0001](0001-zephyros-distribution-of-hermes-agent.md), whose Hermes stays the engine of the Owner and the default of the Contractor

## Context

Zephyros does not try to be a better coding agent. It is the organisation and the independent judge around one: the contract, the rights of each side, the sandbox, the gate, the evidence. Until now every instance ran Hermes Agent ([ADR 0001](0001-zephyros-distribution-of-hermes-agent.md)): one image (`images/instance`), seeded with the model gateway and the MCP server of the hub, and one engine adapter. A team that already works with another coding agent could not bring it, and nothing showed that the guarantees of Zephyros belong to the hub and the platform rather than to Hermes.

The first third-party agent asked for is OpenCode, an open-source coding agent with a non-interactive mode (`opencode run`), an OpenAI-compatible provider, remote MCP servers and a managed configuration scope.

## Decision

**The Contractor instance runs the agent engine the configuration names**: `instances.contractor.engine`, `hermes` (the default) or `opencode`, each in its own image (`images.instance`, `images.opencode`). The Owner instance runs Hermes: its work (the contract, the acceptance tests, the judgment) has been measured on Hermes only, and a second engine on that side is a later decision.

**What holds whatever the engine.** None of it is asked of the agent; all of it is enforced around it.

- The agent never sees a provider key. The model is reached through the gateway of the hub only, with the project token, and the gateway counts every call against the instance that made it (`gateway.call`).
- The agent acts on the hub only through its MCP tools and its project token, with the rights of its side, which do not change: a Contractor can neither write the contract nor change an acceptance test.
- The same container template (user 10000, read-only root, the two volumes of the instance, the limits, no capability), the same network guard and level ([ADR 0024](0024-three-levels-of-network-access.md)), the same sandbox and the same gate. The filter holds each side to the image of its engine, and to the guard and the volumes of the side its turn is labelled with: a Contractor turn that runs OpenCode cannot start the Hermes image, nor the other way round.
- Each start of a turn records the engine of its side and its version (`turn.start`: `engine`, `engine_version`), and the evidence report of a delivery names the engines of the Contractor turns started before the delivery came in. The hub records the engine its configuration gives the side; the filter reads the same configuration and holds the turn to the image of that engine.

**What changes with the engine**: the image, how a turn starts, how the session continues from one turn to the next, how the agent compacts its context, and the agent's own record of its calls. The hub's record does not change: `turn.end`, `gateway.call` and the tool records are the same for both.

**OpenCode in Zephyros.** What follows was checked on the pinned binary, with the obedient stub or a scripted provider as the model.

- **Pinned and baked.** OpenCode 1.19.0, the build the npm registry publishes for the platform (musl, on the Python 3.12 Alpine base of the `tests` kit), fetched by exact version and checked against the SHA-512 the registry gives as its integrity. The npm client never runs. The image needs nothing from the network, so a `sealed` instance works.
- **Configured in its managed scope.** `/etc/opencode/opencode.jsonc`, in the read-only root, which OpenCode reads after every other scope: one provider (`zephyros`, OpenAI-compatible, at the gateway of the hub, with the project token as its key), one MCP server (the hub, with the project token), every tool allowed without a question (a turn cannot answer one; the agent acts inside its container, and what the container reaches is the guard's decision), no sharing, no update, no plugin, no snapshot, no language server, no formatter. The image turns off what OpenCode would otherwise fetch or read by itself: the update check, the model list of models.dev, the default and external plugins, the download of language servers, the configuration, instructions and skills of other agents, and the configuration a delivered repository carries. A sealed turn leaves the counters of its guard at zero.
- **Nothing a turn leaves is loaded by the next.** OpenCode also reads the configuration of its user (`~/.config/opencode`) and a `.opencode` directory in its home: instructions (`AGENTS.md`, `instructions`), agents, commands, skills, plugins and MCP servers, and it adds them to the managed scope rather than replace it (it joins lists such as `instructions`), so a managed key cannot refuse them. On the home volume, the agent of one turn could leave there what every later turn would load, as content the hub treats as untrusted; Hermes turns off its background review for the same reason. The image points both at empty directories of its read-only root (`XDG_CONFIG_HOME`, and `OPENCODE_TEST_HOME`, the name OpenCode 1.19.0 gives the home it reads, which no other program reads). The Bun runtime of OpenCode reads neither the `.env` nor the `bunfig.toml` of the work directory.
- **A turn is `turn <prompt>`.** The entrypoint of the image runs `opencode run` with the prompt on its standard input: never on the command line, where OpenCode would read a prompt that begins with `-` as an option and quote one that holds a space.
- **One session for the instance.** `opencode run --continue` takes the last session of the project OpenCode names the work directory after, and that name follows the first commit of the git repository there. OpenCode moves a session along when `/work` becomes a repository, but a turn that clones another repository into `/work`, or starts its history again, would make the next turn begin a new conversation. The entrypoint therefore finds the root session OpenCode last wrote in its database on the home volume, whatever its project, and continues it with `--session`; without one (the first turn), it falls back to `--continue`, which creates it ([ADR 0016](0016-one-agent-container-per-turn.md)).
- **No call abandoned and paid twice.** The gateway answers once the provider has answered whole, so the headers of a long answer come late. OpenCode gives up on a provider after 300 s without headers, then asks again; the provider would be paid for both. The managed configuration waits 3 660 s, one minute more than the longest `gateway.timeout_seconds` allows: the gateway's limit is the one that holds.
- **Compaction.** OpenCode compacts a session as it nears the context of the model, minus its output. The configuration gives the model a context of 96 000 tokens and an output of 16 000, so compaction starts near 80 000, where Hermes compacts too. (Since [ADR 0033](0033-the-agents-work-with-the-context-of-the-model.md), both compact where the configuration says.)

## Consequences

- Positive: the guarantees of Zephyros are shown to be the hub's and the platform's. The runtime tests that prove them for Hermes have their counterpart for OpenCode (`tests/runtime/test_opencode.py`, `test_opencode_sealed.py`, `test_opencode_wiring.py`).
- Positive: the OpenCode image is about 300 MB, against 3 GB for the Hermes image.
- Negative: two engines to pin, follow and test. A new version of OpenCode is a change of the image, of `OPENCODE_VERSION` and of the documented tool list. The runtime tests read from OpenCode itself the version, the resolved configuration, the tools, the directories it loads, the session it continues and the keys that bound its wait for a provider; `OPENCODE_TEST_HOME` and the session table are OpenCode's internals and may change with it.
- Negative: OpenCode does not read the `AGENTS.md` of the work directory as instructions, since the configuration of a delivered repository is turned off; the agent can still read it as a file.
- Negative: OpenCode asks the model for the title of a new session (one more `gateway.call`, counted like the others), and retries a failed model call several times, about a minute in all, before it ends the turn with an error.
- Negative: in a turn of OpenCode, the user configuration of the other programs (`XDG_CONFIG_HOME`: git, pip) is the same empty, read-only directory. `git config --global` still writes `~/.gitconfig`.
- Not yet shown: the behaviour of a real model through OpenCode, its use of the hub's tools, and the measures of a turn (duration, calls, tokens). Every test runs the obedient stub ([ADR 0002](0002-guarantees-from-os-and-hub.md)) or a scripted provider as the model.
- Not covered by the CI: the arm64 build of OpenCode and its SHA-512, since the CI runner is amd64. A wrong digest stops the build of the image on an arm64 machine; it never lets another file in.

## Alternatives considered

- **`opencode serve`, one server per instance**: a listener inside every instance and work that outlives a turn, which ADR 0016 rules out.
- **`npm install opencode-ai` in the image**: runs the package's install script and needs Node.js, for the same binary.
- **A configuration written into the home volume at each start**, as the Hermes image seeds its own: the agent can rewrite it during a turn. The managed scope in the read-only root cannot be rewritten at all.
- **A read-only home for OpenCode** (`HOME` on the root): the commands of the agent would lose a writable home too. Only the two directories OpenCode reads its configuration from move.
- **The session identifier kept by the hub**: the hub would have to read what OpenCode prints, and the session would still live in OpenCode's database on the home volume. The entrypoint reads it there.
- **The engine reported by the filter at each start**: the most direct witness, at the price of a change to the protocol between the hub and the filter. Both read one configuration, and the filter refuses a turn whose image is not the one of its side's engine.
- **An engine per instance chosen in each project**: the configuration of the hub chooses for every project; a per-project choice can come later without changing the image or the template.
