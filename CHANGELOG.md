# Changelog

The public API of Zephyros — the API of the hub, the command line, the contract schema, the
events schema and the configuration schema — follows semantic versioning
2.0.0 (CTR-13). Before version 1.0, **breaking changes are announced here**, under a
"Breaking changes" heading of the version that carries them; after 1.0 they move the major
version. Each schema carries its own version number.

## [Unreleased]

Nothing yet.

## [0.2.0] - 2026-10-14

Fixes from two independent audits of 0.1.0, network access in three levels, a
verification kit that delivered code cannot forge, and the hardening of the operator channel, the
main-agent endpoint, their tokens and the network guard. Release notes:
[docs/release-notes/0.2.0.md](docs/release-notes/0.2.0.md).

| Surface | 0.1.0 | 0.2.0 |
| --- | --- | --- |
| Configuration schema | 1.12.0 | 1.15.0 |
| Contract schema | 1.1.0 | 1.1.0 |
| Events schema | 1.1.0 | 2.0.0 |
| Verification interface | 0.1.0 | 0.2.0 |
| Forge, agent engine, delivery and model provider interfaces | unchanged | unchanged |

### Breaking changes
- The "tests" verification kit keeps delivered code out of the process that records the results
  (ADR 0017, EXG-37, ATK-22). An acceptance test is black box: it runs the delivery as a
  subprocess and reads its output. A test that imports a module of the delivery, or a support
  module beside it, no longer loads and reads `error` (#11).
- An instance no longer reaches the whole Internet by default (ADR 0024): the default level of
  `network.egress` is `regulated`, the hub and the package registries through its egress proxy.
  Write `egress = "open"` in `[network]` for the former behavior. A guard whose level differs from
  the configuration is stopped when the hub starts or finds it, so the next turn of that instance
  starts with the configured level (#16).
- A delivery is never PASS for want of evidence (ADR 0020 and its amendment). A contract with no
  requirement or constraint, or a delivery where no acceptance test of the judged objects ran and
  passed, is CONCERNS at best, with a reservation that says nothing was judged. A contract judged
  by inspection alone no longer reaches PASS. A conforming inspection still turns CONCERNS into
  PASS once at least one acceptance test ran and passed, and never makes up for a failed test of
  the object it judges (decision D16, recorded in ADR 0020) (#9, #15, #18).
- `contract.import` takes a document whole or not at all. An identifier defined twice, a
  verification method that is not T, I, A or D (`TBD`, `Test`), and a requirement or constraint
  row without its five cells are refused and named with their line, where they used to be
  replaced, read letter by letter, or dropped (#12).
- The events schema is 2.0.0: every notification says `"schema_version": "2.0.0"`. No field is
  added or removed, but two fields take values they never took, which breaks a receiver that
  checked them against a closed list:
  `delivery.decided` is emitted once when a delivery is declared, and again if an inspection
  changes its decision: its `decision` can now be `REFUSED` (the checks refused it) or `ERROR`
  (its verification could not finish), besides `PASS`, `CONCERNS` and `FAIL`. A receiver that
  accepted three values must accept five (#9, #15, #17). The alerts of the continuity ladder can
  carry `side: main-agent`, for a change the Owner side proposed and the main agent is to decide
  (ADR 0023) (#13). A receiver that reads `schema_version` can tell the two apart (#18).
- The operator channel and the main-agent endpoint never listen on an instance network
  ([ADR 0025](docs/decisions/0025-operator-and-main-agent-off-the-instance-networks.md)). The
  configuration schema refuses a wildcard address (`0.0.0.0`, `::`) and an address of the
  instance pool for `hub.operator_bind` and `hub.main_agent_bind`; a hub in a container writes
  `"core"`, as `scripts/deploy.sh` now does. The hub refuses to start when it has not exactly one
  address outside the instance pool; [docs/configuration.md](docs/configuration.md#where-the-operator-channel-and-the-main-agent-endpoint-listen)
  says what to do. The core network of a deployment is now named `zephyros-core-<project>`; the
  former one is left behind: remove it with `docker network rm zephyros_core` (the default project;
  `<project>_core` otherwise) (#19).
- The operator and main-agent tokens expire
  ([ADR 0026](docs/decisions/0026-a-lifetime-and-a-rotation-for-the-operator-and-main-agent-tokens.md)),
  90 days after they are issued by default (`tokens.channel_lifetime_days`). A token issued
  before the update gets that lifetime from the first start of the hub after the update, and
  stops working when it ends (90 days after that start by default). Before that day, run
  `scripts/deploy.sh rotate-token main-agent` and give the main agent the new token; the operator
  token needs nothing, `scripts/deploy.sh call` reads the new one from its file (#19).
- The network guard runs as user 10002, not root
  ([ADR 0027](docs/decisions/0027-the-network-guard-runs-without-root.md), revising ADR 0015),
  without no-new-privileges (for it alone), and needs its image rebuilt, which
  `scripts/deploy.sh up` does. A guard started before the update keeps running as root until its
  instance stops: `scripts/deploy.sh down` then `scripts/deploy.sh up` replaces every guard (the
  instances keep their volumes) (#19).

### Added
- Three levels of network access for instances (ADR 0024, superseding ADR 0009): `regulated`
  reaches the hub and the hosts of `network.allowed_hosts` (by default `pypi.org`,
  `files.pythonhosted.org`, `registry.npmjs.org`) through an egress proxy in the hub, which
  accepts `CONNECT` to port 443 of a listed name only and logs every destination as
  `instance.egress`; `sealed` reaches the hub alone; `open` reaches every public destination,
  with a warning in the log at each turn. The instance image carries pytest, at the versions and
  hashes of the kit, so a `sealed` instance runs its tests (#16).
- The replay tools (`tools/replay/`): an exporter that turns the hub's database into one JSON
  document (format 2.1.0, [docs/replay-format.md](docs/replay-format.md)), with masking and
  renaming; a self-contained player page; a recorder that makes a video of it (#6, #14).
- `acceptance.put` answers `named_by_pending`: the changes that could still be approved and whose
  criteria would name the file (#13).
- The evidence report lists the tests of the Owner's files that no criterion names, which never
  ran, from what the kit reports (`VerificationResult.deselected`), and marks the Owner's files no
  criterion names (#9, #11, #17).
- A record of a delivery carries a `revision`; both stores refuse a write made from a stale read
  (#9).
- `scripts/deploy.sh rotate-token operator|main-agent` (`zephyros hub rotate-token`, which finds
  the control socket in `paths.control_socket`): the running hub writes a new token to its file,
  replaced whole and synced (mode 0600), revokes the old ones and logs both (`token.issue`,
  `token.revoke`, `why: rotation`); the answer names the file, never the token. A token given a
  lifetime at start is logged as `token.lifetime`. An expired operator or main-agent token is
  answered `{"error": "token_expired", "message": ...}`, which names the command (ADR 0026) (#19).

### Changed
- The configuration schema, version 1.13.0: the models the gateway may serve come from the
  configuration alone (`gateway.models`); the code no longer holds a list of authorised models
  (`AUTHORISED_MODELS`, which allowed DeepSeek V4.1 Flash alone, is gone). A configuration that
  names one model authorises that one alone, as the real runs did. When the model sent to the
  provider is not in the list, the refusal names the key that sets it, `gateway.upstream_model`,
  or `gateway.model` when the file has no `gateway.upstream_model`, and lists the models it
  allows. A file accepted by 1.12.0 is accepted by 1.13.0 (#10).
- The configuration schema, version 1.14.0: `network.egress`, `network.allowed_hosts` and
  `hub.egress_port` (ADR 0024). `network.resolvers` serves the `open` level only (#16).
- The configuration schema, version 1.15.0: the value `"core"` for `hub.operator_bind` and
  `hub.main_agent_bind`, and the refusal of a wildcard or instance-pool address there (ADR 0025);
  `tokens.channel_lifetime_days` (90 by default) and `paths.control_socket` (ADR 0026) (#19).
- The verification interface is 0.2.0: `VerificationResult.deselected` lists the tests the kit
  found in the asked files but did not run because no acceptance criterion named them. The kit
  signs it with the outcomes (`ZEPHYROS-OUTCOMES {"deselected": [...], "outcomes": {...}} <hmac>`)
  (#11, #17).
- The filter reads the log of a sandbox from its beginning, as a stream, up to 16 MiB, and believes
  nothing in a log that was cut or that does not begin with the run line of the kit. The kit holds
  the asked test files to the SHA-256 the filter measured on the archive, and imports the bytes it
  read when the run began. The container of a sandbox has a log configuration with no rotation
  (#11).
- A change of the contract awaits whoever is to decide it (ADR 0023). A change the
  Owner side proposed alerts the main agent, then the human; a later proposal no longer resets the
  decision ladder (#13).
- The tree of a delivery is read with one `git cat-file --batch` instead of one git process per
  file, with the same limits; a tree over the size limit is refused before any content is read
  (#12).
- The title of a pull request is built when the worker opens or retitles it, from the record as
  it is then (#9).
- The architecture decisions 0016 to 0019, 0021, 0023, 0024 and the amendment of 0020 are
  Accepted; ADR 0020 records decision D16 on what an inspection can change (#18).
- ADR 0025 to 0027 (the hardening above) and ADR 0028 are Accepted. ADR 0028 records that the
  contract of a project is stored in SQLite as immutable versions, and what is not history: a
  decided change is updated in place, and an acceptance test is overwritten by the next
  `acceptance.put` (the log and the evidence report keep its digest) (#19).

### Fixed
- The continuity ladder wakes the Contractor only once its contract is ready: the version in
  force names an acceptance test, and no change that could still be approved awaits a decision
  (US-E7-01, ADR 0021). The log records when the task is held, and why, and when it is
  released (#7).
- The continuity ladder is kept in the hub's database: a restart keeps each task held or on its
  step, and the hub reads the contract and changes a task under one lock (#13).
- The main agent is told of every documented event of the delivery loop, `delivery.reviewed`
  included, from a queue and a thread of their own: a slow receiver no longer holds up the hub
  (#13).
- A PASS or CONCERNS delivery is told to the main agent even when the record of its pull request
  cannot be written; a refused delivery is told even when the log cannot take its record, and the
  alert for a loop of failed deliveries (EXG-23) still goes out when announcing a FAIL fails
  (#15, #17).
- An inspection recorded while the pull request opens is kept, commented and retitled; it used
  to be overwritten (#9).
- The evidence report and its inspection addendum are kept apart: a test output that prints the
  addendum's heading no longer cuts the flags, reservations and cost, and an inspection rewrites
  one addendum instead of stacking another (#9, #15).
- The masking of secrets (EXG-49) no longer changes ordinary words. Words end with `sk-`
  (`risk-`, `disk-`, `task-`), so a provider key counts only where a token starts, or after an
  escape that test output prints (`\n`, `\t`, `%3D`, an ANSI colour code): `risk-management-module`
  and `src/disk-usage-monitoring.py` stay as written in the log, the evidence report, the comments
  and the retitled title of a pull request. Every other shape is masked wherever it stands. The
  log masks the keys of the parameters as it masks their values (#10).
- The anchor of the log (EXG-19, ATK-69, [ADR 0019](docs/decisions/0019-anchor-outside-the-store.md))
  no longer turns itself off. An anchor that is gone, that cannot be read, or that does not name a
  stream with records fails `log.verify` with a problem that says so (`first_bad_seq` is then
  null), where it used to read as "no anchor". The anchor is never started again from the store,
  and it no longer follows a store whose records were replaced: it moves only from the head it
  names. It starts with the log, and the hub records its start, and any fault it finds when it
  opens the log, as `log.anchor` in its own stream. A deployment whose anchor is lost now sees its
  verification fail: the anchor goes with the database in a backup (#10).
- A refused delivery logs the acceptance test files it changes (20 at most, with their count),
  not only their number (ATK-16) (#9).
- A git error while reading a tree is a refusal that keeps its message (#12).
- A refused MCP tool call carries its corrective action (`action`), as the operator channel does
  (EXG-51); `docs/errors.md` says what each endpoint answers, the model gateway included (#18).

### Security
- An acceptance test file that a criterion names is never taken from the delivery. A file the
  hub does not hold, and the base does not hold either, is missing: the delivered version is set
  aside, its tests are `not_run`, and the report and the log name it. It used to run, and could
  pass (ATK-16) (#9).
- The Contractor reads the evidence report without the output of the run, which quoted the Owner
  side's tests; every per-test outcome stays (`delivery.status`) (#9, #13).
- A pushed branch whose name holds a secret the hub knows, or a string shaped like a credential,
  is refused before the staging area keeps it, so it reaches neither the forge nor the title of a
  pull request (ATK-84, EXG-49) (#9).
- Delivered code cannot rewrite the outcomes the kit records: no delivered module is imported
  into the judge, the judge cannot be read by the processes a test starts, a bytecode cache
  shipped with the delivery is never read, and a flooded or forged log is never believed (ATK-22,
  EXG-37) (#11).
- The operator channel and the main-agent endpoint have a second barrier besides the guards
  (ATK-13, ADR 0025): nothing listens on 8081 or 8082 at an address of an instance network, and
  both refuse, and log (`wrong_source`), a peer whose address is in the instance pool. A runtime
  test proves it on a real deployment with no guard at all (#19).
- The operator and main-agent tokens, which hold every right of their channel, no longer live
  forever: a leaked copy stops working at the end of the lifetime, or at once after a rotation
  (ADR 0026) (#19).
- The network guard is no longer user 0 of the machine: NET_ADMIN reaches its nft alone, through
  a file capability; the runtime tests check that no other file of its image has a capability or
  a setuid bit, and that its main process holds no capability (ADR 0027) (#19).

## [0.1.0] - 2026-10-13

The first release: a hub with no model that runs a software project between an Owner side and a
Contractor side, tried in three runs with real models. Release notes:
[docs/release-notes/0.1.0.md](docs/release-notes/0.1.0.md).

### Added
- The contract schema, version 1.1.0: requirements, constraints, stories, criteria, links and
  notes, with the operator-reserved objects and the acceptance criteria of the Owner side.
- The configuration schema, version 1.12.0: the hub, the networks and resolvers, the limits
  (including the refusals of one instance per minute, ATK-71), the tokens, the images, the
  forge, the verification, the signed notifications (EXG-43) and the continuity ladder
  (US-E7-01); `gateway.simulation` marks the runs with the simulated provider (EXG-46). The
  gateway names the models it may serve (`gateway.models`); the start refuses any model the
  owner's policy does not authorise (`AUTHORISED_MODELS`, at first DeepSeek V4.1 Flash alone).
  The forge opens pull requests on GitHub (`forge.provider` `github`, with the file of its
  token in `forge.token_file`), as well as in a directory. The gateway names the session header
  its provider asks for (`gateway.session_header`, for OpenCode Go's `x-opencode-session`),
  and waits for a long answer of the provider (`gateway.timeout_seconds`, 600 by default).
  The verification names the client's own files (`verification.protected_paths`, by default
  `CONTRACT.md`): a delivery that changes one is flagged. A turn may use 1536 MiB and last two
  hours by default (ADR 0020).
- The Owner side judges with the hub (ADR 0020): it reads the pending changes of the contract
  (`contract.pending`) and the delivered branch (`GET /staging/delivery/<id>`), records an
  inspection (`inspection.record`) and its verdict (`delivery.review`), posted on the pull
  request. The gate judges the constraints as well as the requirements. When an inspection
  changes the decision, the hub retitles the pull request. The forge interface is 0.4.0
  (`comment`, `update_title`).
- The evidence report flags the acceptance test files of the Owner side that no criterion
  names: the gate never runs them. `acceptance.put` answers the criteria that name the file
  (`named_by`), with a warning when there are none.
- The events schema, version 1.1.0: the payloads of the signed notifications
  (`delivery.decided`, `delivery.loop`, `delivery.reviewed`, `continuity.alert_main`,
  `continuity.alert_human`); every payload carries `schema_version`. 1.1.0 added
  `delivery.reviewed` (ADR 0020).
- The command line: `zephyros config check|schema`, `zephyros hub start|call`,
  `zephyros filter start`, `zephyros --version`.
- The API of the hub: the operator channel, the main-agent channel and the agent endpoint,
  with the tool set of `docs/tools.md` and the instance tools of `docs/instance-tools.md`. The
  agent endpoint gives the version of Zephyros in its MCP handshake.
- `scripts/deploy.sh up|down|call` runs Zephyros on one machine; `down` also removes the
  guards, turns and sandboxes, and their networks, and keeps the volumes.

### Breaking changes
- None: 0.1.0 is the first release, and nothing was promised before it. To note all the same
  (ADR 0020): the gate judges the constraints as well as the requirements, and an object
  verified by a method other than test keeps a delivery from PASS until the Owner side records
  its inspection; the rule names in the report say "requirements and constraints".
