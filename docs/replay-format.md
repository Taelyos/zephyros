# Replay format

A replay is one JSON document that tells what happened in one recorded Zephyros run, in order:
the contract and its changes, the turns of the Owner and the Contractor, their model calls and
their refusals, the deliveries, the gate's decisions, the inspections and the verdicts, and what
it all cost. The replay player (`tools/replay/player.html`) plays it back; any other program may
read it too.

The exporter builds a replay from the hub's database:

```sh
uv run --locked python -m tools.replay.export --db hub.db --project p_0123456789 \
  --kind real --from-seq 238 --to-seq 501 --output run.replay.json
```

Current version: **2.2.0**.

## Versioning

`version` follows semantic versioning. A reader accepts every document whose major version it
knows and ignores the fields and event types it does not know. A new optional field or a new
event type is a minor change; renaming or removing a field, or changing its meaning, is a major
change.

## Document

```json
{
  "format": "zephyros-replay",
  "version": "2.2.0",
  "run": { ... },
  "integrity": { ... },
  "pricing": { ... },
  "contract_at_start": { ... },
  "contract": { ... },
  "events": [ ... ],
  "totals": { ... }
}
```

### `run`

| Field | Type | Meaning |
| --- | --- | --- |
| `kind` | `"real"`, `"scripted"` or `"synthetic"` | `real`: real models behind the gateway, a run that actually happened. `scripted`: an attack of the executable catalog (ATK), played by test code against a real hub, with no model. `synthetic`: records that test code wrote into a hub database itself; no run happened and no model answered (the example of the repository). A player must show the kind on every screen, and must not show a kind it does not know as a real run. |
| `title` | string | A short title for the run. |
| `attacks` | list of strings | For a scripted run, the attack identifiers it plays (`ATK-91`). Empty for any other kind. |
| `project` | object | `id`, `name`, `repository` (URL or `null`). |
| `stream` | string | The hub log stream the events come from (the project identifier). |
| `from_seq`, `to_seq` | integers | The first and last log records exported. |
| `started_at`, `ended_at` | strings | ISO 8601 UTC times of the first and last event. |
| `model` | string or `null` | The model behind the gateway, when the exporter was told (`--model`). |
| `exported_at` | string | When the replay was produced. |
| `exporter` | string | The program and its version. |
| `redactions` | list of strings | The kinds of text the exporter replaced, among `token-shaped secrets`, `private IPv4 addresses`, `--redact patterns` and `renamed names`. Never what was replaced. |

### `integrity`

The hub log is a hash chain: each record carries the hash of the one before. The exporter checks
the records it exports against their hashes and their links.

| Field | Type | Meaning |
| --- | --- | --- |
| `chain` | `"verified"`, `"broken"` | `verified`: every exported record matches its hash and links to the one before. |
| `records` | integer | Number of log records checked. |
| `first_previous` | string | The `previous` hash of the first exported record (the link to what came before). |
| `last_hash` | string | The hash of the last exported record. |
| `detail` | string or `null` | Where and why the chain breaks, when it does. |

`verified` says that the exported records are consistent with one another, and nothing more. The
check reads neither the head the hub keeps of the stream nor its anchor outside the store, and
the chain has no key: records cut after the last exported one, or a log rewritten whole with its
hashes recomputed, would still read `verified`. `log.verify` on the hub checks those. Compare
`first_previous` and `last_hash` with the hub's own log to tie a replay to it. The check is the
exporter's, made when it wrote the replay: a player shows its result and checks nothing itself,
so a replay edited by hand would read the same.

Only events whose `source` is `hub-log` are covered by the chain, and of each one only the fields
its record holds (see `data` and `hub_state` below).

### `pricing`

`input_per_million`, `output_per_million` (US dollars per million tokens), `currency` (`"USD"`)
and `note` (where the rates come from: the exporter's default rates unless the command line gave
others). Costs in the replay are computed from these rates; they are indicative.

### `contract_at_start` and `contract`

`contract_at_start` is the contract in force before the first event; `contract` is the one in
force at the end of the exported range. Each is `version` (number), `version_id`, and `objects`,
a list of `{id, type, title, text, priority, verification, test}`, one for every object of the
version: the requirements (`EXG-…`), constraints (`CTR-…`), stories (`US-…`) and criteria, and
also the links (`LNK:<story>><requirement>`, whose two ends are in the identifier) and the notes
(`NB-…`). `verification` is the object's own when it has one (for a criterion, an attack
identifier or a method letter), otherwise its methods joined by commas (`test`,
`inspection,test`); `test` is the acceptance test a criterion names (`path::name`). Fields a given
object lacks are `null`. A run that starts before any contract has `version` and `version_id`
`null` and no objects. Each version written in between is a `contract.version` event.

## Events

`events` is ordered by time, then by `seq`. Every event has these fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | integer | Position in the list, from 0. |
| `t` | string | ISO 8601 UTC time. |
| `offset_ms` | integer | Milliseconds since `run.started_at`. |
| `seq` | integer or `null` | The hub log record, when the event comes from one. |
| `source` | string | `hub-log` (a record of the hash-chained log), `hub-state` (read from the hub's tables: contract versions, the protected paths of an old delivery) or `transcript` (an instance's own conversation, not covered by the chain). |
| `actor` | string | `operator`, `owner`, `contractor`, `hub`, `main-agent`. Only `owner` and `contractor` are sides of the run; the others act on it. |
| `type` | string | See below. For `hub-log` events this is the hub action, verbatim (`gateway.call`, `contract.propose`, ...). |
| `category` | string | One of `contract`, `turn`, `model`, `refusal`, `delivery`, `gate`, `judgment`, `message`, `flag`, `obstacle`, `other` (`obstacle` since 2.2.0; a reader of 2.1 places it as `other`). A player that does not know a `type` can still place it by its category. |
| `outcome` | string | `ok`, `refused` or `error`. |
| `summary` | string | One line, in English, that a player can show as is. It says what happened, whatever the outcome: a failed verification reads as a failure, not as zero tests passed. |
| `data` | object | The details, by type (below). Unknown keys are allowed. |
| `hub_state` | object, optional | On a `hub-log` event only: what the hub's tables add to the record (below). |

For a `hub-log` event, `data` holds what the record holds: its parameters, without the caller's
address and the token identifiers, with a delivery always named `delivery_id` (the gate names it
`delivery`), and the fields the exporter computes from the record alone: `reason` and `action`
of a refusal, `side` and `cost_usd` of a model call, `version` of a contract read (the number its
`version_id` starts with). A reader can check `data` against the record.

`hub_state` holds what the hub's tables add to such an event, as it stood at the time of the
event; the chain does not cover it. The tables keep the last state of a delivery, not its
history, so the exporter adds only what held then: the paths a push changed, the paths a check
flagged, the outcomes the gate decided from, the URL of the pull request on the events of that
pull request, and the observation of an inspection or the reasons of a review on the one record
the row keeps them for (an inspection recorded again replaces the earlier one). An event with
nothing to add has no `hub_state`.

A `hub-state` event is included only when it happened inside the exported time window (from the
time of the first exported record to the time of the last), plus the contract versions that the
exported records themselves created. A run that changes the contract inside the window therefore
shows its new version, even when that version's own time falls at the edge of the range.

### Categories and types

| Category | Types (non-exhaustive) | `data` | `hub_state` |
| --- | --- | --- | --- |
| `contract` | `contract.import`, `contract.apply_reserved`, `contract.propose`, `contract.approve`, `contract.reject`, `contract.pending`, `contract.read`, `contract.version`, `acceptance.put` | `change_id`, `summary`, `operations` (a count), `version_id`, `version`, `pending`; for `contract.version` (source `hub-state`): `number`, `version_id`, `author`, `objects` (as in `contract`, the objects of that version); for `acceptance.put`: `path`, `sha256`. | |
| `turn` | `instance.start`, `turn.start`, `turn.end` | `side`, `prompt_chars`, `result` (an error outcome when the turn did not end well); `error` when a turn could not start; `session`, `continue` or `new` (a new session the operator asked for: always on `turn.start`, on `instance.start` only when new); on `turn.start`, the context of the agents (`context_window`, `context_tokens`, `compaction_tokens`). | |
| `model` | `gateway.call` | `side`, `model`, `prompt_tokens`, `completion_tokens`, `tool_calls`, `cost_usd`; on error, `detail` and no tokens. | |
| `refusal` | any type with outcome `refused` | `reason` (the hub's reason code), `detail`, `action` (the refused action), and the record's other parameters. A refused `delivery.check` carries `paths` (the acceptance test files the delivery changes, the first of them) and `count` (all of them); a hub older than that logged the count alone, as `paths`. | |
| `delivery` | `delivery.push`, `delivery.declare`, `delivery.status`, `staging.prepare`, `staging.read`, `verification.run`, `pull_request.open`, `pull_request.comment`, `pull_request.retitle`, and `delivery.check` when it flags no protected path | `delivery_id`, `branch`, `changed` (a count), `bytes`, `passed`, `tests`, `number`; on `verification.run`, `timed_out` (the tests that reached a time limit of their own) and `stopped_at_seconds` (the limit of a run stopped at it); on error, `error`; for `delivery.check`, as in `flag`. | `changed_paths` on `delivery.push`; `url` on `pull_request.*`. |
| `gate` | `gate.decision` | `delivery_id`, `decision` (`PASS`, `CONCERNS`, `FAIL`), `contract_version`; after an inspection, `after` and `previous` (the decision before). | `outcomes` (test id to result). |
| `judgment` | `inspection.record`, `delivery.review` | `delivery_id`, `object_id`, `verdict`, `decision` (the gate's decision after the record). | `observation` or `reasons`. |
| `flag` | `delivery.check`, `delivery.flag` | `delivery.check` (source `hub-log`): the hub flagged a delivery that changes protected paths, `delivery_id`, `flagged` (a count), and, from a hub that logs them, `missing` (the acceptance test files the contract names that nobody put) and `set_aside` (the delivered files at those paths, never run). A check that only reports `missing` files, with `flagged` 0, is no flag: its category is `delivery`. `delivery.flag` (source `hub-state`): a run older than that rule, where the exporter found the protected paths among the delivered ones, `delivery_id`, `paths`, `rule` (`protected-path`), `by` (`exporter`). | `paths` on `delivery.check`. |
| `obstacle` | `obstacle.report`, `obstacle.answer`, `obstacle.close`, `obstacle.read`, `obstacle.list` ([ADR 0034](decisions/0034-a-blocked-agent-reports-an-obstacle-up-a-chain.md)) | `obstacle_id`; on a report, `nature`, `blocking`, `title`, `level` (who is to answer: `owner`, `operator`, `human`) and `evidence` (the kinds of its items); on an answer, `decision` (`clarify`, `amend`, `fix_test`, `handle`, `reject`, `escalate`), `status`, `level` and `from_level` (where it stood before), `text_chars`, and `change_id`, `paths` or `action` with the decision they go with; on a close, `status` and `note_chars`. The texts of an obstacle and of its answers stay in the hub: `obstacle.read` shows them. A `notification.sent` of the event `obstacle.raised` (category `other`) tells that one reached the operator or the human. | |
| `message` | `agent.message` | `side`, `text` (possibly shortened, see `truncated`), `truncated`, `role` (`final` for the last answer of a turn, `note` otherwise). A message the state database holds in several sessions appears once. | |
| `other` | `project.create`, `token.issue`, `token.revoke`, `notification.sent`, `continuity.step`, ... | The record's parameters. | |

Every event of category `refusal` keeps the refused action in `data.action`: its `type` is the
action as the hub logged it (`unknown` when the call matched no tool). A refusal can come from
any actor: the hub refuses a delivery that changes an acceptance test (`delivery.check`) or a
connection outside its allow-list (`egress.refused`), and the operator can be refused too.

### `totals`

| Field | Meaning |
| --- | --- |
| `events` | Number of events. |
| `model_calls` | `{owner: n, contractor: n}`: the model calls that were answered, as the hub counts them in its evidence report. A side with none is absent. |
| `tokens` | `{owner: {input, output}, contractor: {...}}`, for the same sides. |
| `cost_usd` | `{owner: x, contractor: y, total: z}`, for the same sides. |
| `refusals` | `{actor: n}`, for every actor refused at least once, the hub and the operator included. |
| `decisions` | The gate decisions in order: `[{delivery_id, decision, t}]`. |
| `verdicts` | The recorded verdicts: `[{delivery_id, verdict, t}]`. |
| `duration_ms` | `ended_at` minus `started_at`. |

## Command line

The exporter is one command. `--db` and `--project` are required; everything else has a default.

```sh
uv run --locked python -m tools.replay.export --db hub.db --project p_0123456789 \
  --output run.replay.json --html run.html
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--db PATH` | — (required) | The hub's SQLite file. It is opened read-only. |
| `--project ID` | — (required) | The project identifier; also the hub log stream the events come from. |
| `--kind {real,scripted,synthetic}` | `real` | See `run.kind`. |
| `--attack ATK-ID` | none | For a scripted run, an attack identifier to name in `run.attacks` (repeatable). |
| `--title TEXT` | the project's name | The title shown for the run. |
| `--model NAME` | none | The model behind the gateway, written to `run.model`. |
| `--from-seq N` | the first record | The first log record to export. |
| `--to-seq N` | the last record | The last log record to export. |
| `--transcript SIDE=PATH` | none | An instance's Hermes state database, with `SIDE` in `owner`\|`contractor` (repeatable). |
| `--redact REGEX` | none | A pattern to replace by `[redacted]` (repeatable). |
| `--rename OLD=NEW` | none | Replace a name by a neutral one wherever it appears (repeatable). |
| `--rename-file PATH` | none | A file of `OLD=NEW` lines (`#` comments) to rename. |
| `--protected-path PATH` | `CONTRACT.md` | A path a delivery must not change; a directory ends with `/` (repeatable). Used only for a run older than the hub's own flag. |
| `--message-chars N` | `1200` | The longest transcript message kept; a longer one is cut and marked `truncated`. |
| `--input-price N` | `0.15` | US dollars per million prompt tokens, for the indicative costs. |
| `--output-price N` | `0.60` | US dollars per million completion tokens. |
| `--price-note TEXT` | says where the rates come from | Written to `pricing.note`. |
| `--output PATH` | standard output | Where to write the replay JSON. |
| `--html PATH` | none | Also write the player, with the replay embedded in it. |

## What a replay never carries

The exporter drops the network address of each caller (`source`), the token identifiers and the
prompts themselves (only their length and digest are logged by the hub). Every text of the
replay — summaries, data, transcripts, contract objects, and the keys of a dictionary that maps
data (the outcomes of the tests, the flagged objects of a change) — then goes through the same
replacements; the names of the fields never do (`--rename delivery=item` changes the summaries,
not the field `delivery_id`):

- a string shaped like a token (provider keys, forge tokens, hub tokens, cloud keys, private-key
  headers) becomes `[masked]`, as the hub masks its own log (EXG-49): transcripts, observations,
  reasons and contract texts are not in the log, so the exporter masks them itself;
- a private IPv4 address (10/8, 100.64/10, 172.16/12, 192.168/16) and every pattern given with
  `--redact` become `[redacted]`;
- `--rename OLD=NEW` replaces a name by a neutral one, so that a run can be published without
  naming the services it touched: `--rename prometheus=service-1 --rename grafana=service-2`, or
  `--rename-file names.txt`, a text file of one `OLD=NEW` per line where a line starting with `#`
  is a comment. A rename is case-insensitive and a name stops where letters and digits stop:
  `prometheus` matches `Prometheus`, and inside `test_prometheus_status` and `PROMETHEUS_URL`,
  never inside `myprometheus` or `prometheus2`.

Two keys of one dictionary that become equal (two addresses redacted) are told apart by a number,
`[redacted] (2)`, so no value is lost. `run.redactions` lists the kinds that matched, never the
patterns or the names: a `--redact` pattern is often the very name to hide.

These replacements are a net, not a review. Read a replay before publishing it: transcripts are
free text, and a secret the hub knows by name but that has no token shape is not found here.

## Playing and recording a replay

To play a replay, open the HTML the exporter wrote with `--html`: it is the player with the
replay embedded, so a double-click is enough. Without `--html`, open `tools/replay/player.html`
in a browser and drop the `.replay.json` file onto the page (or use its file picker). To try it,
drop `tools/replay/example.replay.json`: a synthetic run, written by test code into a hub
database and exported by the exporter (`tests/unit/tools/replay_example.py` tells how; a test
keeps the file equal to a new export).

The player shows the run's kind and title, the integrity of the log chain, the contract as it
stands at each moment, the two sides' lanes, the gate, the verdict, the flags, the costs and the
events in order. It can seek, focus an event, change the speed and show a caption. An event it
cannot show is reported in the browser console and skipped; the replay goes on. These URL
parameters set it up:

| Parameter | Meaning |
| --- | --- |
| `src=URL` | Fetch the replay from `URL` (a page opened from `file://` cannot fetch; embed or drop instead). |
| `from=ID`, `to=ID` | Play only the events from `ID` to `ID` (event identifiers). |
| `speed=N` | The playback speed (`1` is real time; long idle gaps are shortened unless unticked). |
| `autoplay=1` | Start playing at once. |
| `captions=0` | Never show a caption. |

A page drives the player through `window.replay`: `play()`, `pause()`, `seek(id)`, `focus(id)`,
`setSpeed(n)`, `caption(text)` and `state()` (`{eventId, playing, done}`).

To record a replay as a video, use `tools/replay/record.mjs` with Node 22, headless Chromium and
`ffmpeg`:

```sh
node tools/replay/record.mjs PAGE.html SCRIPT.json OUT.mp4 [--gif OUT.gif]
```

`PAGE.html` is a player with its replay embedded (`--html`); `SCRIPT.json` is a list of steps run
in order against the player (`seek`, `focus`, `speed`, `play`, `pause`, `wait`, `until`, `caption`).
The recording stops with an error when the page fails or when an `until` step does not reach its
event in time. Read the header comment of `record.mjs` for the exact shape of a step and the
other options.
