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

Current version: **1.0.0**.

## Versioning

`version` follows semantic versioning. A reader accepts every document whose major version it
knows and ignores the fields and event types it does not know. A new optional field or a new
event type is a minor change; renaming or removing a field, or changing its meaning, is a major
change.

## Document

```json
{
  "format": "zephyros-replay",
  "version": "1.0.0",
  "run": { ... },
  "integrity": { ... },
  "pricing": { ... },
  "contract": { ... },
  "events": [ ... ],
  "totals": { ... }
}
```

### `run`

| Field | Type | Meaning |
| --- | --- | --- |
| `kind` | `"real"` or `"scripted"` | `real`: real models behind the gateway, a run that actually happened. `scripted`: an attack of the executable catalog (ATK), played by test code against a real hub, with no model. A player must show the kind on every screen. |
| `title` | string | A short title for the run. |
| `attacks` | list of strings | For a scripted run, the attack identifiers it plays (`ATK-91`). Empty for a real run. |
| `project` | object | `id`, `name`, `repository` (URL or `null`). |
| `stream` | string | The hub log stream the events come from (the project identifier). |
| `from_seq`, `to_seq` | integers | The first and last log records exported. |
| `started_at`, `ended_at` | strings | ISO 8601 UTC times of the first and last event. |
| `model` | string or `null` | The model behind the gateway, when the exporter was told (`--model`). |
| `exported_at` | string | When the replay was produced. |
| `exporter` | string | The program and its version. |
| `redactions` | list of strings | What the exporter replaced (for example `private IPv4 addresses`, or `renamed names` when a `--rename` matched). |

### `integrity`

The hub log is a hash chain (each record carries the hash of the one before). The exporter
checks the records it exports against their hashes.

| Field | Type | Meaning |
| --- | --- | --- |
| `chain` | `"verified"`, `"broken"` | `verified`: every exported record matches its hash and links to the one before. |
| `records` | integer | Number of log records checked. |
| `first_previous` | string | The `previous` hash of the first exported record (the link to what came before). |
| `last_hash` | string | The hash of the last exported record. |
| `detail` | string or `null` | Where and why the chain breaks, when it does. |

Only events whose `source` is `hub-log` are covered by the chain.

### `pricing`

`input_per_million`, `output_per_million` (US dollars per million tokens), `currency` (`"USD"`)
and `note` (where the rates come from). Costs in the replay are computed from these rates; they
are indicative.

### `contract`

The contract as it stood at the end of the exported range: `version` (number), `version_id`,
and `objects`, a list of `{id, type, title, text, priority, verification}` for the requirements
(`EXG-…`), constraints (`CTR-…`), stories (`US-…`) and criteria. Fields a given object lacks
are `null`.

## Events

`events` is ordered by time, then by `seq`. Every event has these fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | integer | Position in the list, from 0. |
| `t` | string | ISO 8601 UTC time. |
| `offset_ms` | integer | Milliseconds since `run.started_at`. |
| `seq` | integer or `null` | The hub log record, when the event comes from one. |
| `source` | string | `hub-log` (a record of the hash-chained log), `hub-state` (read from the hub's tables: contract versions, deliveries) or `transcript` (an instance's own conversation, not covered by the chain). |
| `actor` | string | `operator`, `owner`, `contractor`, `hub`, `main-agent`. |
| `type` | string | See below. For `hub-log` events this is the hub action, verbatim (`gateway.call`, `contract.propose`, ...). |
| `category` | string | One of `contract`, `turn`, `model`, `refusal`, `delivery`, `gate`, `judgment`, `message`, `flag`, `other`. A player that does not know a `type` can still place it by its category. |
| `outcome` | string | `ok`, `refused` or `error`. |
| `summary` | string | One line, in English, that a player can show as is. |
| `data` | object | The details, by type (below). Unknown keys are allowed. |

A `hub-state` event is included only when it happened inside the exported time window (from the
time of the first exported record to the time of the last), plus the contract versions that the
exported records themselves created. A run that changes the contract inside the window therefore
shows its new version, even when that version's own time falls at the edge of the range.

### Categories and types

| Category | Types (non-exhaustive) | `data` |
| --- | --- | --- |
| `contract` | `contract.import`, `contract.apply_reserved`, `contract.propose`, `contract.approve`, `contract.reject`, `contract.pending`, `contract.read`, `contract.version`, `acceptance.put` | `change_id`, `summary`, `operations`, `version_id`, `version`; for `contract.version` (source `hub-state`): `number`, `author`, `objects` (as in `contract`, the objects of that version); for `acceptance.put`: `path`, `sha256`. |
| `turn` | `instance.start`, `turn.start`, `turn.end` | `side`, `prompt_chars`, `result`. |
| `model` | `gateway.call` | `side`, `prompt_tokens`, `completion_tokens`, `tool_calls`, `cost_usd`; on error, `detail`. |
| `refusal` | any type with outcome `refused` | `reason` (the hub's reason code), `detail`, `action` (the refused action). |
| `delivery` | `delivery.push`, `delivery.declare`, `delivery.status`, `delivery.check`, `staging.prepare`, `staging.read`, `verification.run`, `pull_request.open`, `pull_request.comment` | `delivery_id`, `branch`, `changed_paths`, `passed`, `tests`, `number`, `url`. |
| `gate` | `gate.decision` | `delivery_id`, `decision` (`PASS`, `CONCERNS`, `FAIL`), `contract_version`, `outcomes` (test id to result, when known). |
| `judgment` | `inspection.record`, `delivery.review` | `delivery_id`, `object_id`, `verdict`, `observation` or `reasons`, `decision` (the gate's decision after the record). |
| `flag` | `delivery.flag` | `delivery_id`, `paths`, `rule` (`protected-path`), `by` (`hub` when the hub flagged it, `exporter` when the exporter derived it from the delivered paths of a run older than the rule). |
| `message` | `agent.message` | `side`, `text` (possibly shortened, see `truncated`), `truncated`, `role` (`final` for the last answer of a turn, `note` otherwise). |
| `other` | `token.issue`, `notification.sent`, ... | The record's parameters. |

Every event of category `refusal` keeps the refused action in `data.action`: its `type` is the
action as the hub logged it (`unknown` when the call matched no tool).

### `totals`

| Field | Meaning |
| --- | --- |
| `events` | Number of events. |
| `model_calls` | `{owner: n, contractor: n, ...}` |
| `tokens` | `{owner: {input, output}, contractor: {...}}` |
| `cost_usd` | `{owner: x, contractor: y, total: z}` |
| `refusals` | `{owner: n, contractor: n, ...}` |
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
| `--db PATH` | — (required) | The hub's SQLite file. |
| `--project ID` | — (required) | The project identifier; also the hub log stream the events come from. |
| `--kind {real,scripted}` | `real` | `real`: real models. `scripted`: an attack of the catalog, no model. |
| `--attack ATK-ID` | none | For a scripted run, an attack identifier to name in `run.attacks` (repeatable). |
| `--title TEXT` | the project's name | The title shown for the run. |
| `--model NAME` | none | The model behind the gateway, written to `run.model`. |
| `--from-seq N` | the first record | The first log record to export. |
| `--to-seq N` | the last record | The last log record to export. |
| `--transcript SIDE=PATH` | none | An instance's Hermes state database, with `SIDE` in `owner`\|`contractor` (repeatable). |
| `--redact REGEX` | none | A pattern to replace by `[redacted]` (repeatable). |
| `--rename OLD=NEW` | none | Replace a name by a neutral one wherever it appears (repeatable). |
| `--rename-file PATH` | none | A file of `OLD=NEW` lines (`#` comments) to rename. |
| `--protected-path PATH` | `CONTRACT.md` | A path a delivery must not change; a directory ends with `/` (repeatable). |
| `--message-chars N` | `1200` | The longest transcript message kept; a longer one is cut and marked `truncated`. |
| `--input-price N` | `0.15` | US dollars per million prompt tokens, for the indicative costs. |
| `--output-price N` | `0.60` | US dollars per million completion tokens. |
| `--price-note TEXT` | a default note | Where the rates come from, written to `pricing.note`. |
| `--output PATH` | standard output | Where to write the replay JSON. |
| `--html PATH` | none | Also write the player, with the replay embedded in it. |

## What a replay never carries

The exporter drops the network address of each caller (`source`), the token identifiers and the
prompts themselves (only their length and digest are logged by the hub). It replaces private
IPv4 addresses (10/8, 100.64/10, 172.16/12, 192.168/16) and any pattern given with `--redact` by
`[redacted]`, and lists what it replaced in `run.redactions`.

`--rename OLD=NEW` replaces a name by a neutral one wherever it appears in the replay —
summaries, data, transcripts, contract objects — so that a run can be published without naming
the services it touched: `--rename prometheus=service-1 --rename grafana=service-2`, or
`--rename-file names.txt`, a text file of one `OLD=NEW` per line where a line starting with `#`
is a comment. A rename is case-insensitive and matches whole words: `prometheus` matches
`Prometheus` and `prometheus`, never `myprometheus`. When any rename matched, the kinds list
holds `renamed names`; the names that were replaced are never written anywhere in the replay.

The hub already masks secrets in its log (EXG-49). Read a replay before publishing it all the
same: transcripts are free text.

## Playing and recording a replay

To play a replay, open the HTML the exporter wrote with `--html`: it is the player with the
replay embedded, so a double-click is enough. Without `--html`, open `tools/replay/player.html`
in a browser and drop the `.replay.json` file onto the page (or use its file picker). The player
shows the run's kind and title, the integrity of the log chain, the contract, the events in
order and the totals, and it can seek, focus an event, change the speed and show a caption.

To record a replay as a video, use `tools/replay/record.mjs` with Node 22, headless Chromium and
`ffmpeg`:

```sh
node tools/replay/record.mjs PAGE.html SCRIPT.json OUT.mp4 [--gif OUT.gif]
```

`PAGE.html` is a player with its replay embedded (`--html`); `SCRIPT.json` is a list of steps run
in order against the player (`seek`, `focus`, `speed`, `play`, `pause`, `wait`, `until`, `caption`).
Read the header comment of `record.mjs` for the exact shape of a step and the other options.
