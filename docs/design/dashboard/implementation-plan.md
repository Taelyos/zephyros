# The dashboard: implementation plan

Five tickets realise [ADR 0021](../../decisions/0021-read-only-dashboard-on-its-own-listener.md).
Each one owns its files: no file appears in two tickets, so they can be worked on at the same
time and merged in any order that respects the dependencies below. The interfaces between them
are fixed here, before any of them starts; a ticket that needs another one's code works against
these signatures and rebases when it lands.

| Ticket | What | Size | Needs |
| --- | --- | --- | --- |
| D1 | Views and events in the core | M | pull request #6 merged (its translator moves here) |
| D2 | Sign-in codes and sessions | M | — |
| D3 | The dashboard listener | L | D1 and D2 interfaces; merges after them |
| D4 | The page | L | the JSON shapes of D1 (below); merges after D3 |
| D5 | Replays on the same page | M | D1 and D4 |

Sizes: S is half a day of one worker, M a day, L two days. D1, D2 and D4 start together; D3 starts
with them against the interfaces and merges last of the four; D5 follows D1 and D4.

## Interfaces fixed before work starts

```python
# zephyros/core/events.py (D1): one log record as one event of the replay format,
# {"id", "t", "seq", "source": "hub-log", "actor", "type", "category", "outcome", "summary", "data"}
EVENTS_FORMAT = "zephyros-replay"


def translate(record: AuditRecord) -> dict[str, object]: ...


# zephyros/core/views.py (D1): read-only views, JSON-ready
class ReadModel(Protocol): ...  # read methods of the existing stores and of the log, nothing else


def projects(read: ReadModel) -> dict[str, object]: ...
def overview(read: ReadModel, project_id: str) -> dict[str, object]: ...
def delivery(read: ReadModel, project_id: str, delivery_id: str) -> dict[str, object]: ...
def contract(read: ReadModel, project_id: str) -> dict[str, object]: ...
def turns(read: ReadModel, project_id: str) -> dict[str, object]: ...
def refusals(read: ReadModel, project_id: str) -> dict[str, object]: ...
def events(read: ReadModel, project_id: str, after: int, limit: int) -> dict[str, object]: ...


# zephyros/core/dashboard_auth.py (D2)
class DashboardAuth:
    def __init__(
        self, clock: Clock, entropy: Callable[[int], bytes] = secrets.token_bytes
    ) -> None: ...
    def issue_code(self) -> str: ...  # one use, 120 s, 5 attempts
    def sign_in(self, code: str) -> SignIn: ...  # SignIn(session: str | None, reason: Reason)
    def session(self, session_id: str) -> Session | None: ...  # touches the idle expiry
    def sign_out(self, session_id: str) -> None: ...


# Session(principal="operator", started_at, expires_at)  — a stream closes at expires_at
```

Log actions added by D2: `dashboard.code`, `dashboard.session` (sign-in, sign-out, expiry),
`dashboard.request` (a refused request of a session; one summary per minute and reason for the
requests refused before any session). Reasons added by D2: `cross_site`, `bad_code`.

HTTP routes served by D3, on port 8083 of the loopback interface:

| Route | Session | Answer |
| --- | --- | --- |
| `GET /`, `GET /static/…` | no | the page and its files (they hold no data) |
| `POST /session` `{"code": …}` | no | sets the cookie; 204, or 403 and a reason |
| `DELETE /session` | yes | signs out |
| `GET /api/projects` | yes | `views.projects` |
| `GET /api/projects/{id}` | yes | `views.overview` |
| `GET /api/projects/{id}/contract`, `/turns`, `/refusals` | yes | the views of the same names |
| `GET /api/projects/{id}/deliveries/{delivery}` | yes | `views.delivery` |
| `GET /api/projects/{id}/events?after=&limit=` | yes | `views.events` |
| `GET /api/projects/{id}/stream` | yes | Server-Sent Events: `id:` the sequence number, `data:` one event |

The JSON shapes follow what the mockup derives in the browser (`js/model.js`, `stateAt`): D1
writes them down in `docs/dashboard-api.md` on its first day, and D4 builds against that file
with fixtures, not against D1's code.

## D1 — Views and events in the core

The read side of the dashboard, in the core (standard library only), and the gate's table kept
with each decision.

Files: `src/zephyros/core/events.py` (new), `src/zephyros/core/views.py` (new),
`src/zephyros/core/deliveries.py`, `src/zephyros/adapters/sqlite/delivery_store.py`,
`src/zephyros/adapters/memory/delivery_store.py`, `docs/dashboard-api.md` (new),
`tests/unit/core/test_events.py` (new), `tests/unit/core/test_views.py` (new),
`tests/unit/core/test_deliveries.py`, `tests/unit/adapters/test_sqlite_delivery_store.py`.

Acceptance criteria:

- `translate` gives an event for every `Action` and every `Outcome`; a test walks both enums. Its
  summaries are right for `ok`, `refused` and `error` alike, and an event carries only what its
  record carries (no state read later is pasted on a past event).
- The translator starts from the one of the fixed exporter of pull request #6 (ported, not
  reinvented), with its own tests here; D5 then removes the exporter's copy.
- Each gate decision is kept with its table by requirement (verified, awaiting, why, tests and
  their outcomes) and its rule checks, in the delivery record. A decision made before this change
  is recomputed by `core.gate` from the recorded results and marked `recomputed`.
- `views.delivery` gives the steps from push to verdict, each decision with its table and rules,
  the tests and outcomes, the inspections, the verdict, the changed paths and the flagged ones.
  `views.overview` gives each side's state (idle, or at work since when), its calls, tokens and
  cost, the current decision, the verdict and every requirement's state. `views.refusals` groups by
  family and by burst, with the families of ADR 0021.
- A run built with the acceptance `World` gives views equal to a reference file kept in the tests.
  A secret planted in a branch name, an observation and a test's output never comes out unmasked;
  no view carries a token, a prompt or the content of an acceptance test.
- Core purity, strict types and 85 % branch coverage hold.

## D2 — Sign-in codes and sessions

Files: `src/zephyros/core/dashboard_auth.py` (new), `src/zephyros/core/audit.py`,
`src/zephyros/core/hub.py`, `src/zephyros/core/rights.py`, `docs/tools.md` (regenerated),
`tests/unit/core/test_dashboard_auth.py` (new),
`tests/acceptance/test_atk_66_dashboard_sessions.py` (new).

Acceptance criteria:

- `dashboard.code` exists for the operator only: it is not listed to, and is refused for, the
  main agent and the instances (the rights matrix test and `docs/tools.md` say so).
- A code works once, expires after 120 seconds and is burnt after five wrong attempts; codes and
  sessions are compared in constant time and kept as hashes.
- A session expires after 30 minutes without use and after 12 hours in all; signing out ends it at
  once; a session identifier chosen by the client is never adopted (fixation).
- No code and no session identifier appears in a log record, an error or a response other than
  the one that issues it (a test scans the log after a run of sign-ins).
- The core part of ATK-66 is tested here: brute force, an expired session, a fixed session, an
  instance or main-agent token presented as a code.

## D3 — The dashboard listener

Files: `src/zephyros/adapters/http/dashboard.py` (new), `src/zephyros/app/hub.py`,
`src/zephyros/core/config.py`, `schemas/configuration.schema.json` (generated),
`docs/configuration.md`, `deploy/compose.yaml`, `scripts/deploy.sh`, `docs/running.md`,
`tests/acceptance/world.py`, `tests/unit/adapters/http/test_dashboard.py` (new),
`tests/acceptance/test_atk_64_65_67_dashboard.py` (new), `tests/runtime/test_dashboard_reach.py`
(new).

Acceptance criteria:

- The routes above. Every one but the page, its files and `POST /session` refuses a request with
  no valid session, the stream included.
- A foreign `Host` is refused; a data or stream request that is cross-site (`Sec-Fetch-Site`,
  `Origin`) is refused; sign-in and sign-out need JSON and the dashboard's own `Origin`; no CORS
  header is ever sent. Every response carries the headers of ADR 0021; data is `no-store`.
- The stream sends `id:` the sequence number and resumes after `Last-Event-ID`; it sends a
  heartbeat every 15 seconds; it closes when its session expires or is signed out; one reader per
  project polls the log every second. At most 4 streams per session and 16 in all.
- Refusals are logged as ADR 0021 says, the anonymous ones as one summary per minute and reason.
- Configuration 1.13.0: `[dashboard]` with `enabled` (default true) and `port` (default 8083); the
  ports of the four listeners differ; an address other than loopback is refused at startup.
  `deploy/compose.yaml` publishes `127.0.0.1:${ZEPHYROS_DASHBOARD_PORT:-8083}`;
  `scripts/deploy.sh dashboard` prints the address and a fresh code.
- ATK-13 still passes unchanged, and the runtime suite shows that a turn of a real deployment
  cannot reach the dashboard's port on the hub (the check of ATK-39, on port 8083; it needs the
  heavy runtime suite). ATK-64, ATK-65 and ATK-67 (the server side: JSON only, the policy, no HTML
  from data) are tested, and the new attacks 1, 2, 3 and 6 of ADR 0021.

## D4 — The page

The screens of the mockup, made production code: they read the API instead of a replay.

Files: `src/zephyros/adapters/http/dashboard_static/` (new: `index.html`, `style.css`,
`favicon.svg`, `js/*.js`), `tests/browser/` (new: `conftest.py`, the tests, fixtures and an
in-page test harness), `pyproject.toml` (the `browser` marker, and the static files in the wheel if
the build backend needs telling).

Acceptance criteria:

- The overview, the drawers (delivery, turn, contract, refusals, requirement, key moments), the
  sign-in page and a project list, on fixtures in the shapes of `docs/dashboard-api.md`.
- A replay mode: the same page plays a replay file (format 1.x) without the API, deriving the
  views from its events as the mockup does, with the transport and the `window.replay` interface
  of the mockup. Its scripts can be embedded in one HTML file without a bundler (D5 does it).
- Live: `EventSource` on the stream, resumed by the browser after a cut; the views an event
  touches are fetched again; nothing flickers or jumps while the human reads a drawer.
- No `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval`, `new Function`,
  inline handler or inline style in the sources: a test of the suite reads them. With the strings
  of ATK-67 in every field of the fixtures, the page shows them as text, raises no
  `securitypolicyviolation`, creates no element from them and requests nothing outside its origin
  (the new attacks 4 and 5 of ADR 0021 are tested here, on the page side).
- Accessibility: every action can be done with the keyboard; a drawer takes and gives back the
  focus; a new decision is announced; the colours of the mockup's README are kept, or checked
  again with the same method; no status by colour alone; `prefers-reduced-motion` honoured. If
  EXG-55 is tested with axe-core, its file is vendored for the tests only and pinned by hash.
- At 1920 × 1080 the overview fits the screen without scrolling, with no text under 13 px; at
  1366 × 768 nothing overflows sideways.
- The browser tests run headless Chromium through its command line (`--dump-dom` of the in-page
  harness) from `pytest`; with no Chromium on the machine they are skipped as "not applicable".

## D5 — Replays on the same page

Files: `tools/replay/` (the exporter, the recorder; `player.html` removed),
`docs/replay-format.md`, `tests/unit/tools/`, `README.md` (the demo and "No dashboard yet"),
`CHANGELOG.md`.

Acceptance criteria:

- The exporter uses `zephyros.core.events.translate`: for the same records, the exporter's events
  and the stream's are equal (a test).
- `gate.decision` events carry the gate's table and rules; the format's version moves by semver
  and `docs/replay-format.md` says what changed.
- `--html` writes one file: the page of D4 in replay mode with the replay embedded, under a
  policy that allows its one inline script by hash and nothing else. The replay mode opens files
  only outside the dashboard's origin (new attack 7 of ADR 0021).
- `record.mjs` records a clip of the third real run through the unchanged `window.replay`
  interface; `player.html` and its tests are gone.

## Not in this plan

Passkey sign-in and listening on a private network (with signed approvals, ADR 0007); the
agents' words in the live view (a separate decision about the gateway); a light theme; approvals
or any other action from the dashboard.
