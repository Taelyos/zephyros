# Changelog

The public API of Zephyros — the API of the hub, the command line, the contract schema, the
events schema and the configuration schema (annex "Public API") — follows semantic versioning
2.0.0 (CTR-13). Before version 1.0, **breaking changes are announced here**, under a
"Breaking changes" heading of the version that carries them; after 1.0 they move the major
version. Each schema carries its own version number.

## [Unreleased]

### Added
- The contract schema, version 1.1.0: requirements, constraints, stories, criteria, links and
  notes, with the operator-reserved objects and the acceptance criteria of the Owner side.
- The configuration schema, version 1.11.0: the hub, the networks and resolvers, the limits
  (including the refusals of one instance per minute, ATK-71), the tokens, the images, the
  forge, the verification, the signed notifications (EXG-43) and the continuity ladder
  (US-E7-01); `gateway.simulation` marks the runs with the simulated provider (EXG-46). The
  gateway names the models it may serve (`gateway.models`); the start refuses any model the
  owner's policy does not authorise (`AUTHORISED_MODELS`, at first DeepSeek V4.1 Flash alone).
  The forge opens pull requests on GitHub (`forge.provider` `github`, with the file of its
  token in `forge.token_file`), as well as in a directory. The gateway names the session header
  its provider asks for (`gateway.session_header`, for OpenCode Go's `x-opencode-session`),
  and waits for a long answer of the provider (`gateway.timeout_seconds`, 600 by default).
- The events schema, version 1.0.0: the payloads of the signed notifications
  (`delivery.decided`, `delivery.loop`, `continuity.alert_main`, `continuity.alert_human`);
  every payload carries `schema_version`.
- The command line: `zephyros config check|schema`, `zephyros hub start|call`,
  `zephyros filter start`, `zephyros --version`.
- The API of the hub: the operator channel, the main-agent channel and the agent endpoint,
  with the tool set of `docs/tools.md` and the instance tools of `docs/instance-tools.md`.

### Breaking changes
- None yet: version 0.0.0 is the first milestone and nothing has been promised before it.
