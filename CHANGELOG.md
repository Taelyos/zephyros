# Changelog

The public API of Zephyros — the API of the hub, the command line, the contract schema, the
events schema and the configuration schema (annex "Public API") — follows semantic versioning
2.0.0 (CTR-13). Before version 1.0, **breaking changes are announced here**, under a
"Breaking changes" heading of the version that carries them; after 1.0 they move the major
version. Each schema carries its own version number.

## [Unreleased]

### Breaking changes
- The "tests" verification kit keeps delivered code out of the process that records the results
  (ADR 0017, EXG-37, ATK-22). An acceptance test is black box: it runs the delivery as a
  subprocess and reads its output. A test that imports a module of the delivery no longer loads
  and reads `error`.

### Changed
- The configuration schema, version 1.13.0: the models the gateway may serve come from the
  configuration alone (`gateway.models`); the code no longer holds a list of authorised models
  (`AUTHORISED_MODELS`, which allowed DeepSeek V4.1 Flash alone, is gone). A configuration that
  names one model authorises that one alone, as the real runs did. When the model sent to the
  provider is not in the list, the refusal names the key that sets it, `gateway.upstream_model`,
  or `gateway.model` when the file has no `gateway.upstream_model`, and lists the models it
  allows. A file accepted by 1.12.0 is accepted by 1.13.0.
- The verification interface is 0.2.0: `VerificationResult.deselected` lists the tests the kit
  found in the asked files but did not run because no acceptance criterion named them. The kit
  signs it with the outcomes (`ZEPHYROS-OUTCOMES {"deselected": [...], "outcomes": {...}} <hmac>`).
- The filter reads the log of a sandbox from its beginning, as a stream, up to 16 MiB, and believes
  nothing in a log that was cut or that does not begin with the run line of the kit. The kit holds
  the asked test files to the SHA-256 the filter measured on the archive, and imports the bytes it
  read when the run began. The container of a sandbox has a log configuration with no rotation.

### Fixed
- The masking of secrets (EXG-49) no longer changes ordinary words. Words end with `sk-`
  (`risk-`, `disk-`, `task-`), so a provider key counts only where a token starts, or after an
  escape that test output prints (`\n`, `\t`, `%3D`, an ANSI colour code): `risk-management-module`
  and `src/disk-usage-monitoring.py` stay as written in the log, the evidence report, the comments
  and the retitled title of a pull request. Every other shape is masked wherever it stands. The
  log masks the keys of the parameters as it masks their values.
- The anchor of the log (EXG-19, ATK-69, [ADR 0019](docs/decisions/0019-anchor-outside-the-store.md))
  no longer turns itself off. An anchor that is gone, that cannot be read, or that does not name a
  stream with records fails `log.verify` with a problem that says so (`first_bad_seq` is then
  null), where it used to read as "no anchor". The anchor is never started again from the store,
  and it no longer follows a store whose records were replaced: it moves only from the head it
  names. It starts with the log, and the hub records its start, and any fault it finds when it
  opens the log, as `log.anchor` in its own stream. A deployment whose anchor is lost now sees its
  verification fail: the anchor goes with the database in a backup.
- The continuity ladder wakes the Contractor only once its contract is ready: the version in
  force names an acceptance test, and no change that could still be approved awaits a decision
  (US-E7-01, ADR 0021). The log records when the task is held, and why, and when it is
  released (#7).

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
