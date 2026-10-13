# 0021. A read-only dashboard on its own listener of the hub, fed by the log as a stream of events

- Status: Proposed
- Date: 2026-10-14

## Context

The human follows a project by reading the log (`log.read`), by calling the state tools, or by exporting a replay of a finished run (the replay format, exporter and player of pull request #6). There is no dashboard. The operator channel refuses browsers on purpose: a request with an `Origin`, a cookie or a `Sec-Fetch-*` header is refused before any token is read (`adapters/http/operator.py`), so that no web page can drive it.

The contract already sets the envelope. The dashboard is the web interface of the hub, through which the human follows projects and later signs approvals. EXG-20 (P0) asks for authentication on each access; EXG-39 (P0) for the web protections of its annex: an authenticated session, CSRF protection, Host and Origin checks against DNS rebinding, output encoding of all displayed text, private interfaces by default. EXG-28 asks to display the verification state of each requirement for each milestone, EXG-55 for WCAG 2.2 AA. Threats T-17 and T-18 and attacks ATK-64 to ATK-67 describe how it is attacked; AS-06 assumes passkey authentication.

The hub already holds what the dashboard needs, except one thing. Each project has a hash-chained log stream in which every call and every refusal is a record, with secrets masked (EXG-19, EXG-49); the state has the projects, the contract versions and pending changes, the deliveries with their outcomes, inspections, verdict and report; the gate (`core.gate`) decides from recorded results; every model call carries its tokens. What it does not hold is the agents' own words: they live in each instance's Hermes session, which only the replay exporter reads, after the run.

An audit of pull request #6 found its format and exporter sound, and its player too dense to read. The mockup in [`docs/design/dashboard/`](../design/dashboard/) shows the screens this record describes, on the third real run.

## Decision

**What it shows.** The first increment is read-only. A project's overview is built to be read in five seconds, and in a 1080p video: the gate's current decision with its trail and the Owner's verdict; each side, idle or at work and for how long, with its calls, tokens and cost; the cost per role; a timeline of turns, refused calls, contract versions and decisions; every requirement and constraint with its verification state. Details open on demand: a delivery (from push to verdict, each requirement at each decision, the gate's rules, the Owner's tests and their outcomes, the inspections, the changed paths and flags), a turn (the input tokens of each model call, the hub's records of the turn), the contract (versions, proposals and their decisions, flagged terms), the refusals by family (out of role, identity, malformed calls, network, limits) and by burst. Text written by an agent is shown as quoted speech, labelled with its side, never in the style of a hub fact; a decision or a verdict is drawn only from the hub's structured fields. Never shown: tokens, secrets, prompts (the log keeps only their length and digest), the content of acceptance tests (names and digests only), the Markdown of the evidence report (the same facts are drawn from structured data).

**Where the data comes from.** From the hub's stores and log, read inside the hub through a view module of the core (standard library only) that has no write path. The table by requirement and the rules of each decision are the gate's own: recorded with the decision and sent as data; the browser never computes them. One event model serves live and replay: the replay format (`zephyros-replay` 1.x). A single translator from a log record to an event (category, actor, outcome, summary) is shared by the hub and the exporter. Live, the page reads the views (overview, delivery, contract, turns, refusals) and then follows a stream of new events. A replay file holds the events of a finished run; the same page plays it and derives the views from the events, as the mockup does, except the gate's table, which the events carry.

**Live, by a stream.** Server-Sent Events, one stream per project. The id of each event is its log sequence number: a browser that reconnects sends `Last-Event-ID` and resumes after the last record it has. One reader per project looks for new records every second and feeds its streams; the write path of the log does not change. The page fetches again the views an event touches. `GET …/events?after=<seq>` serves the same events to a client that cannot keep a stream. The agents' words are not in the live view: the hub does not hold them, and reading an instance's session would mean parsing a file an agent writes. Replays keep them, marked as outside the hash chain.

**How it is served.** By the hub process, on a fourth listener, `dashboard` (port 8083 by default): its own Starlette application, sharing no route with the agent endpoint, the operator channel or the main-agent endpoint. Like the operator channel, it is published on the loopback interface of the machine only. Inside the instance networks, to which the hub is attached, the network guards let through the agent endpoint and nothing else (ADR 0015), so no instance reaches the new port; the listener would refuse an instance's token and a foreign `Host` all the same. The page is static HTML, CSS and JavaScript modules, shipped as package data of the hub: no build step, no framework, no CDN, no font or image from elsewhere.

**Authentication.** Every route but the sign-in page and its static files requires a session, the stream included (EXG-20, T-17). To sign in, the operator asks the operator channel for a one-time code (a tool `dashboard.code`, operator only; `scripts/deploy.sh dashboard` prints it) and types it into the sign-in page. The code is eight characters (40 random bits), used once, valid for two minutes and burnt after five wrong attempts; the hub keeps only its hash, and takes at most ten sign-in attempts a minute in all. A session is 256 random bits in a cookie `HttpOnly; SameSite=Strict; Path=/` (with `Secure` once the listener serves TLS), kept in the hub's memory, with an idle expiry of 30 minutes and an absolute one of 12 hours; signing out or restarting the hub ends it. No credential is read from a URL, and no token of the hub is accepted, whether operator, main agent or instance (ATK-66).

**Web protections (EXG-39).** A request whose `Host` is not one of the listener's names is refused (DNS rebinding, ATK-64). A data or stream request that says it is cross-site (`Sec-Fetch-Site`) or carries a foreign `Origin` is refused. The two requests that change state, sign-in and sign-out, need a JSON body and the dashboard's own `Origin` (CSRF). No CORS header is sent. Every response carries `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`, with `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy: same-origin` and `Cross-Origin-Resource-Policy: same-origin`; data responses are `Cache-Control: no-store`.

**Untrusted text (T-18, ATK-67).** The server sends JSON only. The page builds its DOM from text nodes and a closed list of attributes: never `innerHTML`, never Markdown, no inline script or style. The only link it makes from data is a pull request on the configured forge's https host. Agent text is isolated for bidirectional rendering, so that it cannot reorder the text around it.

**What is logged.** On the hub stream: each sign-in, accepted or refused, each sign-out and expiry, and each refused request of a session. Requests refused before any session (a foreign `Host`, no cookie) are counted and written as one summary record per minute and reason, so that a page that floods the listener cannot flood the log; whether such a request is a "refusal of an action" under EXG-19 is the Owner's to confirm. A read is not logged: it changes nothing, a stream reads every second, and the session record says who could read and when. The rule "every call leaves one record" concerns the tools, which the dashboard does not call.

**Beyond loopback.** The configuration refuses a non-loopback address for this listener until sign-in by passkey exists. AS-06 assumes passkey authentication; passkeys come with signed approvals (ADR 0007, EXG-17), and the dashboard will use them to sign in before it listens on a private network. Until then, a remote screen reaches the dashboard through an SSH tunnel.

**Accessibility (EXG-55).** A status is never told by colour alone: an icon and a word go with it. The colours are chosen and checked for contrast (text at least 4.5:1) and for the common colour-vision deficiencies; every screen works with the keyboard, a drawer keeps the focus and gives it back; a new decision is announced to screen readers; motion stops when the system asks for less of it.

**Threat model.** ATK-13 extends to the new port: from every execution context, a connection to it is blocked by the guard, and the hub-side half asserts that the agent endpoint still answers on its own routes only. ATK-64 to ATK-67 become testable with this increment, for its read-only routes. New attacks are proposed for the catalogue; their numbers are the Owner's to give:

1. A stream that outlives its session (expired, signed out, hub restarted) keeps delivering events.
2. A page or a local process floods the listener with refused requests to fill the hash-chained log.
3. Many streams, very large pages of events or slow readers exhaust the hub's memory or tasks.
4. An agent writes text that imitates a hub fact (a branch named `PASS ✓`, a summary saying "Gate: PASS", bidirectional or invisible characters) to make a delivery look accepted.
5. A secret that reached an agent's text (a test's output, a message, an observation) is shown unmasked (EXG-49 end to end).
6. Another page frames the dashboard to make the human click in it (clickjacking).
7. A crafted replay file is opened where the dashboard's session lives.

**The replay player of pull request #6.** Its format stays and becomes the event model of the dashboard. Its exporter stays, as the path from a finished run to a file, and moves to the shared translator. Its player is replaced by the page of the dashboard in replay mode: the same screens and a transport, with the same `window.replay` interface, so that `tools/replay/record.mjs` keeps recording videos. The replay mode runs only outside the dashboard's origin (a file, a static site), because a replay file is untrusted input. The fixes being made to pull request #6 carry over: format, exporter, masking and recorder.

The implementation is planned in five tickets with disjoint files: [`docs/design/dashboard/implementation-plan.md`](../design/dashboard/implementation-plan.md).

## Consequences

- Positive: a run can be followed as it happens, decision first and evidence one click away; EXG-28 gets the table of each requirement at each decision; refusals are told apart (out of role, or a mistake).
- Positive: live view, replays and, later, AgoraCraft read one event model; the hub and the exporter cannot drift apart in how they name what happened.
- Positive: no new dependency and no build chain; the page holds no token and cannot run injected script (a strict policy, a DOM made of text).
- Negative: the hub grows a listener that faces a browser, which is security-critical code (ADR 0003). It is read-only, kept apart from the other three listeners, and its attacks join the catalogue.
- Negative: sessions live in memory: a restart of the hub signs the human out, and a new code is needed.
- Negative: the live view shows what the hub records, not what the agents say; only replays show their words. A later decision may let the gateway keep a bounded, masked excerpt of each answer.
- Negative: loopback only, until passkeys; another machine needs a tunnel.

## Alternatives considered

- A separate process reading the hub's database read-only: it isolates the hub from the page, but its refusals could not join the hub's chain, sign-in would need a second trust root, read-only SQLite in WAL mode across containers is fragile on some runtimes, and the contract makes the dashboard the hub's own interface.
- The page calling the operator channel: the channel refuses browsers on purpose, and a page holding the operator token would hold every right.
- Polling only: simpler, but a stream resumes by sequence number at no cost and keeps one connection; polling stays as the fallback.
- WebSocket: nothing flows from the page to the hub, and the hub's server runs without WebSocket support.
- A front-end framework and a build chain: more supply chain to pin (CTR-10) and review, for a handful of screens.
- Pages rendered on the server: a second place where escaping must be right (T-18), and replays need a page that works without a server anyway.
- Reading the agents' transcripts live from the instance volumes: the hub would parse files that an agent writes.
- Passkey sign-in in this increment: a cryptography dependency and a ceremony for a view that changes nothing; it comes with approvals.
