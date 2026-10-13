// The read model of the mockup: a replay document (format "zephyros-replay" 1.x) turned into
// what the screens show, at any instant of the run. In the dashboard itself these views come from
// the hub (ADR 0021); here they are derived in the browser from a recorded run.

import { SIDE_NAME, VERDICT_WORDS, plural, roleName } from './format.js';

export const SIDES = ['owner', 'contractor'];

/** Refusal reasons of the hub, by family: what a viewer needs first is "was it out of role?". */
export const FAMILIES = [
  { key: 'role', label: 'Out of role', hint: 'a right the caller does not have',
    reasons: ['forbidden', 'other_project', 'self_approval', 'reserved_data', 'wrong_endpoint', 'protected_path'] },
  { key: 'identity', label: 'Identity', hint: 'token, address, host, expiry',
    reasons: ['unauthenticated', 'unknown_token', 'wrong_source', 'expired', 'revoked', 'browser_request', 'bad_host'] },
  { key: 'malformed', label: 'Malformed calls', hint: 'no such tool, path or field',
    reasons: ['invalid_request', 'not_found', 'unknown_action', 'not_pending', 'stale_base', 'unreadable'] },
  { key: 'network', label: 'Network', hint: 'egress outside the policy', reasons: ['egress'] },
  { key: 'limits', label: 'Limits and other', hint: 'rate limit, busy', reasons: ['rate_limited', 'busy'] },
];
const FAMILY_OF = new Map(FAMILIES.flatMap((f) => f.reasons.map((r) => [r, f.key])));
export const familyOf = (reason) => FAMILY_OF.get(reason) || 'limits';

export const DECISION_STATUS = { PASS: 'good', CONCERNS: 'warning', FAIL: 'critical', ERROR: 'serious' };
export const DECISION_ICON = { PASS: 'checkCircle', CONCERNS: 'half', FAIL: 'crossCircle', ERROR: 'alert' };
export const VERDICT_STATUS = { accept: 'good', accept_with_reservations: 'warning', reject: 'critical' };
export const STATE_LOOK = {
  verified: { status: 'good', icon: 'checkCircle', word: 'Verified' },
  awaiting: { status: 'warning', icon: 'half', word: 'Awaits inspection' },
  failed: { status: 'critical', icon: 'crossCircle', word: 'Not verified' },
  unknown: { status: 'neutral', icon: 'ring', word: 'See the report' },
  pending: { status: 'neutral', icon: 'ring', word: 'Not judged yet' },
};

const str = (v) => (typeof v === 'string' ? v : v === null || v === undefined ? '' : String(v));
const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
const obj = (v) => (v && typeof v === 'object' && !Array.isArray(v) ? v : {});
const list = (v) => (Array.isArray(v) ? v : []);
const failedResult = (result) => typeof result === 'string' && result.startsWith('failed');
const versionNumber = (versionId) => { const m = /^v(\d+)-/.exec(str(versionId)); return m ? Number(m[1]) : null; };

/** Validate and normalise a replay document. Throws an Error with a readable message. */
export function loadReplay(doc) {
  if (!doc || typeof doc !== 'object' || Array.isArray(doc)) throw new Error('The document is not a JSON object.');
  if (doc.format !== 'zephyros-replay') throw new Error('This is not a Zephyros replay (format).');
  const major = str(doc.version).split('.')[0];
  if (major !== '1') throw new Error(`Unsupported replay version ${str(doc.version)}: this page reads 1.x.`);
  if (!Array.isArray(doc.events)) throw new Error('The replay has no list of events.');

  const run = obj(doc.run);
  const seen = new Set();
  const events = [];
  for (const raw of doc.events) {
    if (!raw || typeof raw !== 'object') continue;
    const e = {
      id: Number.isInteger(raw.id) ? raw.id : events.length,
      ms: num(raw.offset_ms), t: str(raw.t), seq: Number.isInteger(raw.seq) ? raw.seq : null,
      source: str(raw.source), actor: str(raw.actor), type: str(raw.type), category: str(raw.category),
      outcome: str(raw.outcome) || 'ok', summary: str(raw.summary), data: obj(raw.data),
    };
    if (e.type === 'agent.message') {
      // The exporter of a transcript may give the same message twice: keep one.
      const key = `${e.actor}|${e.t}|${str(e.data.text)}`;
      if (seen.has(key)) continue;
      seen.add(key);
    }
    events.push(e);
  }
  events.sort((a, b) => a.ms - b.ms); // stable: ties keep the order of the log
  const durationMs = num(obj(doc.totals).duration_ms) || (events.length ? events[events.length - 1].ms : 0);
  return build({ run, integrity: obj(doc.integrity), pricing: obj(doc.pricing), contract: obj(doc.contract), events, durationMs });
}

function build(m) {
  const { events } = m;
  m.byId = new Map(events.map((e) => [e.id, e]));
  m.calls = events.filter((e) => e.type === 'gateway.call');
  m.turns = buildTurns(events);
  m.versions = buildVersions(m);
  m.changes = buildChanges(events);
  m.deliveries = buildDeliveries(events);
  m.refusals = events.filter((e) => e.outcome === 'refused' || e.category === 'refusal').map((e) => ({
    e, ms: e.ms, actor: e.actor, reason: str(e.data.reason) || 'unknown', detail: str(e.data.detail),
    action: str(e.data.action) || e.type, requested: str(e.data.requested), family: familyOf(str(e.data.reason)),
  }));
  m.bursts = buildBursts(m.refusals);
  m.lastActions = Object.fromEntries(SIDES.map((side) => [side, events.filter(
    (e) => e.actor === side && e.source === 'hub-log' && e.type !== 'gateway.call')]));
  m.moments = buildMoments(m);
  return m;
}

// ------------------------------------------------------------------------------------- turns
function buildTurns(events) {
  const turns = [];
  const open = {};
  for (const e of events) {
    const side = str(e.data.side);
    if (e.type === 'turn.start' && SIDES.includes(side)) {
      const starter = [...events].reverse().find((x) => x.type === 'instance.start' && x.ms <= e.ms && str(x.data.side) === side);
      const turn = {
        key: `turn-${e.id}`, side, n: turns.filter((t) => t.side === side).length + 1,
        start: e.ms, end: null, result: null, startedBy: starter ? starter.actor : null,
        promptChars: num(e.data.prompt_chars), calls: [], actions: [], refusals: [], messages: [],
      };
      turns.push(turn);
      open[side] = turn;
    } else if (e.type === 'turn.end' && open[side]) {
      open[side].end = e.ms;
      open[side].result = str(e.data.result) || e.outcome;
      open[side] = null;
    }
  }
  for (const e of events) {
    const side = SIDES.includes(e.actor) ? e.actor : null;
    if (!side) continue;
    const turn = turns.find((t) => t.side === side && e.ms >= t.start && (t.end === null || e.ms <= t.end));
    if (!turn) continue;
    if (e.type === 'gateway.call') turn.calls.push(e);
    else if (e.outcome === 'refused') turn.refusals.push(e);
    else if (e.type === 'agent.message') turn.messages.push(e);
    else if (e.source === 'hub-log') turn.actions.push(e);
  }
  for (const turn of turns) {
    turn.failed = failedResult(turn.result);
    turn.modelErrors = turn.calls.filter((c) => c.outcome === 'error');
    turn.label = turnLabel(turn);
  }
  return turns;
}

function turnLabel(turn) {
  const ok = (type) => turn.actions.filter((e) => e.type === type && e.outcome === 'ok');
  if (turn.failed) {
    return turn.modelErrors.length ? 'Failed: the provider refused a call' : `Failed (${turn.result})`;
  }
  const parts = [];
  const declared = ok('delivery.declare').map((e) => str(e.data.delivery_id));
  const reviewed = ok('delivery.review').map((e) => str(e.data.delivery_id));
  const inspections = ok('inspection.record').length;
  const tests = new Set(ok('acceptance.put').map((e) => str(e.data.path)));
  const proposals = ok('contract.propose').length;
  if (declared.length) parts.push(`Built and delivered ${declared.join(', ')}`);
  if (reviewed.length) parts.push(`Reviewed ${reviewed.join(', ')}`);
  if (inspections) parts.push(plural(inspections, 'inspection'));
  if (reviewed.length) parts.push('verdict');
  if (tests.size && !reviewed.length) parts.push(`Wrote ${plural(tests.size, 'acceptance test')}`);
  if (proposals) parts.push(proposals === 1 ? 'proposed a change' : `proposed ${proposals} changes`);
  if (!parts.length) parts.push(turn.end === null ? 'At work' : plural(turn.calls.length, 'model call'));
  return parts.join(' · ');
}

// ---------------------------------------------------------------------------------- contract
function buildVersions(m) {
  const versions = m.events.filter((e) => e.type === 'contract.version').map((e) => ({
    number: num(e.data.number), versionId: str(e.data.version_id), author: str(e.data.author),
    ms: e.ms, objects: list(e.data.objects).map(obj),
  }));
  if (!versions.length && Array.isArray(m.contract.objects)) {
    versions.push({ number: num(m.contract.version), versionId: str(m.contract.version_id), author: '', ms: 0, objects: m.contract.objects.map(obj) });
  }
  versions.sort((a, b) => a.number - b.number);
  versions.forEach((v, i) => {
    const before = i ? new Map(versions[i - 1].objects.map((o) => [o.id, JSON.stringify(o)])) : null;
    const now = new Map(v.objects.map((o) => [o.id, JSON.stringify(o)]));
    v.added = before ? [...now.keys()].filter((id) => !before.has(id)) : [];
    v.removed = before ? [...before.keys()].filter((id) => !now.has(id)) : [];
    v.changed = before ? [...now.keys()].filter((id) => before.has(id) && before.get(id) !== now.get(id)) : [];
  });
  return versions;
}

function buildChanges(events) {
  const changes = [];
  const byId = new Map();
  for (const e of events) {
    const d = e.data;
    if (e.outcome !== 'ok') continue;
    if (e.type === 'contract.apply_reserved' || e.type === 'contract.import') {
      changes.push({ kind: e.type === 'contract.import' ? 'import' : 'reserved', by: e.actor, ms: e.ms, summary: str(d.summary),
        operations: num(d.operations), flagged: obj(d.flagged), status: 'applied', version: versionNumber(d.version_id) });
    } else if (e.type === 'contract.propose') {
      const change = { kind: 'proposal', id: str(d.change_id), by: e.actor, ms: e.ms, summary: str(d.summary),
        operations: num(d.operations), flagged: obj(d.flagged), base: num(d.base_version), status: 'pending' };
      changes.push(change);
      byId.set(change.id, change);
    } else if (e.type === 'contract.approve' || e.type === 'contract.reject') {
      const change = byId.get(str(d.change_id));
      if (!change) continue;
      change.status = e.type === 'contract.approve' ? 'approved' : 'rejected';
      change.resolvedBy = str(d.approver) || e.actor;
      change.resolvedMs = e.ms;
      change.version = versionNumber(d.version_id);
    }
  }
  return changes;
}

/** Requirements and constraints of a version, with a human name taken from the story covering them. */
export function judgedObjects(version) {
  if (!version) return [];
  const byId = new Map(version.objects.map((o) => [str(o.id), o]));
  const covering = new Map();
  for (const o of version.objects) {
    const match = /^LNK:([^>]+)>(.+)$/.exec(str(o.id));
    if (match) covering.set(match[2], [...(covering.get(match[2]) || []), match[1]]);
  }
  return version.objects.filter((o) => o.type === 'requirement' || o.type === 'constraint').map((o) => {
    const stories = (covering.get(o.id) || []).map((id) => byId.get(id)).filter(Boolean);
    const firstSentence = str(o.text).split(/(?<=\.)\s/)[0];
    const methods = str(o.verification).split(/[,\s]+/).filter(Boolean);
    return {
      id: str(o.id), type: o.type, priority: str(o.priority), methods, text: str(o.text),
      name: stories.length ? str(stories[0].title) : firstSentence,
      stories: stories.map((st) => ({ id: str(st.id), title: str(st.title), text: str(st.text),
        criteria: version.objects.filter((c) => c.type === 'criterion' && str(c.id).startsWith(`${st.id}.`)) })),
    };
  }).sort((a, b) => a.type.localeCompare(b.type) * -1 || a.id.localeCompare(b.id, 'en', { numeric: true }));
}

export function versionAt(m, ms) {
  let current = m.versions[0] || null;
  for (const v of m.versions) if (v.ms <= ms) current = v;
  return current;
}

// -------------------------------------------------------------------------------- deliveries
function buildDeliveries(events) {
  const map = new Map();
  const get = (id) => {
    if (!map.has(id)) {
      map.set(id, { id, branch: '', changedPaths: [], pushed: null, declared: null, verification: null,
        decisions: [], pr: null, comments: [], inspections: [], reviews: [], refusedReviews: [], flags: [] });
    }
    return map.get(id);
  };
  let latest = null;
  for (const e of events) {
    const id = str(e.data.delivery_id);
    if (!id) {
      // A refused review names no delivery: it belongs to the delivery under judgment.
      if (e.type === 'delivery.review' && e.outcome === 'refused' && latest) latest.refusedReviews.push(e);
      continue;
    }
    const d = get(id);
    if (e.type === 'delivery.push' && e.outcome === 'ok') {
      d.pushed = e; d.branch = str(e.data.branch); d.changedPaths = list(e.data.changed_paths).map(str); latest = d;
    } else if (e.type === 'delivery.declare' && e.outcome === 'ok') {
      d.declared = e; d.branch = d.branch || str(e.data.branch); latest = d;
    } else if (e.type === 'verification.run') {
      d.verification = e;
    } else if (e.type === 'gate.decision') {
      d.decisions.push({ e, ms: e.ms, decision: str(e.data.decision), after: str(e.data.after) || null,
        previous: str(e.data.previous) || null, outcomes: obj(e.data.outcomes), contractVersion: num(e.data.contract_version), inspections: [] });
    } else if (e.type === 'pull_request.open' && e.outcome === 'ok') {
      // Only the record that opens the pull request carries its address at that instant.
      d.pr = { number: num(e.data.number), url: str(e.data.url), ms: e.ms };
    } else if (e.type === 'pull_request.comment' && e.outcome === 'ok') {
      d.comments.push(e);
    } else if (e.type === 'inspection.record' && e.outcome === 'ok') {
      const record = { e, ms: e.ms, object: str(e.data.object_id), verdict: str(e.data.verdict),
        observation: str(e.data.observation), decisionAfter: str(e.data.decision) };
      d.inspections.push(record);
      // The hub logs the new decision just before the inspection that caused it.
      if (d.decisions.length) d.decisions[d.decisions.length - 1].inspections.push(record);
    } else if (e.type === 'delivery.review' && e.outcome === 'ok') {
      d.reviews.push({ e, ms: e.ms, verdict: str(e.data.verdict), reasons: str(e.data.reasons), decision: str(e.data.decision) });
    } else if (e.type === 'delivery.flag') {
      d.flags.push(e);
    }
  }
  return [...map.values()].filter((d) => d.pushed || d.declared);
}

/**
 * The state of each requirement and constraint at one gate decision.
 *
 * The replay holds each test's outcome, not the gate's table by requirement, so the mockup
 * approximates: an object verified by test is verified when every acceptance test passed; one
 * verified by another method needs an inspection that conforms. The dashboard takes the table
 * from the gate itself (core.gate), never from a re-implementation in the browser.
 */
export function judge(objects, decision, inspections) {
  const outcomes = Object.values(decision ? decision.outcomes : {});
  const allPassed = outcomes.length > 0 && outcomes.every((o) => o === 'passed');
  return objects.map((o) => {
    if (!decision) return { ...o, state: 'pending', inspection: null };
    let state = 'verified';
    if (o.methods.includes('test') && !allPassed) state = 'unknown';
    const others = o.methods.filter((x) => x !== 'test');
    let inspection = null;
    if (others.length) {
      for (const r of inspections) if (r.object === o.id) inspection = r;
      if (!inspection) state = state === 'verified' ? 'awaiting' : state;
      else if (inspection.verdict !== 'conforms') state = 'failed';
    }
    return { ...o, state, inspection };
  });
}

export function rulesOf(judged) {
  const group = (name, sub, required, items) => ({
    name, sub, required, total: items.length,
    verified: items.filter((j) => j.state === 'verified').length,
    awaiting: items.filter((j) => j.state === 'awaiting').length,
  });
  return [
    group('P0', 'every one', 100, judged.filter((j) => j.priority === 'P0')),
    group('P1', 'at least 90 %', 90, judged.filter((j) => j.priority === 'P1')),
    group('Overall', 'at least 80 %', 80, judged),
  ];
}

/** The inspections that count for decision `index` of a delivery (they accumulate). */
export function inspectionsUpTo(delivery, index) {
  return delivery.decisions.slice(0, index + 1).flatMap((d) => d.inspections);
}

// ---------------------------------------------------------------------------------- refusals
function buildBursts(refusals) {
  const bursts = [];
  for (const r of refusals) {
    const last = bursts[bursts.length - 1];
    if (last && last.actor === r.actor && last.reason === r.reason && last.detail === r.detail && r.ms - last.endMs <= 60000) {
      last.items.push(r);
      last.endMs = r.ms;
    } else {
      bursts.push({ actor: r.actor, reason: r.reason, detail: r.detail, family: r.family, ms: r.ms, endMs: r.ms, items: [r] });
    }
  }
  return bursts;
}

// ------------------------------------------------------------------------------- key moments
function buildMoments(m) {
  const out = [];
  const add = (e, status, iconName, who, text, open, ms, detail) => out.push({ ms: ms ?? e.ms, id: e.id, actor: e.actor, status, icon: iconName, who, text, open, detail: detail || '' });
  const putBurst = [];
  const flushPuts = () => {
    if (!putBurst.length) return;
    const first = putBurst[0];
    const files = new Set(putBurst.map((e) => str(e.data.path)));
    add(first, 'neutral', 'flask', SIDE_NAME[first.actor] || first.actor, `stores ${plural(files.size, 'acceptance test')}`, { kind: 'turn', at: first.ms, side: first.actor }, putBurst[putBurst.length - 1].ms);
    putBurst.length = 0;
  };
  for (const e of m.events) {
    if (e.type === 'acceptance.put' && e.outcome === 'ok') {
      if (putBurst.length && e.ms - putBurst[putBurst.length - 1].ms > 60000) flushPuts();
      putBurst.push(e);
      continue;
    }
    if (putBurst.length && e.ms - putBurst[putBurst.length - 1].ms > 60000) flushPuts();
    const d = e.data;
    const who = SIDE_NAME[e.actor] || e.actor;
    switch (e.type) {
      case 'staging.prepare':
        if (e.outcome === 'ok') add(e, 'neutral', 'box', 'Hub', `prepares the base: ${str(d.branch)} at ${str(d.base).slice(0, 7)}`, null);
        else add(e, 'serious', 'alert', 'Hub', 'cannot prepare the base from the forge', null, undefined, str(d.error));
        break;
      case 'turn.start': {
        const turn = m.turns.find((t) => t.start === e.ms && t.side === str(d.side));
        if (turn) add(e, 'neutral', 'play2', SIDE_NAME[turn.side], `turn ${turn.n} begins`, { kind: 'turn', key: turn.key });
        break;
      }
      case 'turn.end': {
        const turn = m.turns.find((t) => t.end === e.ms && t.side === str(d.side));
        if (!turn) break;
        if (turn.failed) add(e, 'serious', 'alert', SIDE_NAME[turn.side], `turn ${turn.n} fails: ${turn.modelErrors.length ? 'the provider answered HTTP 400' : turn.result}`, { kind: 'turn', key: turn.key });
        else add(e, 'neutral', 'check', SIDE_NAME[turn.side], `turn ${turn.n} ends · ${turn.label.toLowerCase()}`, { kind: 'turn', key: turn.key });
        break;
      }
      case 'contract.apply_reserved':
        if (e.outcome === 'ok') add(e, 'neutral', 'doc', who, `applies a reserved change → contract v${versionNumber(d.version_id) ?? '?'}`, { kind: 'contract' });
        break;
      case 'contract.propose':
        if (e.outcome === 'ok') add(e, 'neutral', 'doc', who, `proposes a change: ${str(d.summary)}`, { kind: 'contract' });
        break;
      case 'contract.approve':
        if (e.outcome === 'ok') add(e, 'good', 'check', who, `approves the Owner's change → contract v${versionNumber(d.version_id) ?? '?'}`, { kind: 'contract' });
        break;
      case 'contract.reject':
        if (e.outcome === 'ok') add(e, 'critical', 'cross', who, `rejects change ${str(d.change_id)}`, { kind: 'contract' });
        break;
      case 'delivery.declare':
        if (e.outcome === 'ok') add(e, 'neutral', 'box', who, `declares delivery ${str(d.delivery_id)} · ${plural(list(d.changed_paths).length, 'changed path')}`, { kind: 'delivery', id: str(d.delivery_id) });
        break;
      case 'verification.run': {
        const passed = num(d.passed), total = num(d.tests);
        if (e.outcome === 'ok') add(e, passed === total ? 'good' : 'critical', 'flask', 'Hub', `replays the Owner's tests on ${str(d.delivery_id)}: ${passed} of ${total} pass`, { kind: 'delivery', id: str(d.delivery_id) });
        else add(e, 'serious', 'alert', 'Hub', `cannot verify ${str(d.delivery_id)}`, { kind: 'delivery', id: str(d.delivery_id) });
        break;
      }
      case 'gate.decision': {
        const after = str(d.after) === 'inspection' ? ' after an inspection' : '';
        add(e, DECISION_STATUS[str(d.decision)] || 'neutral', DECISION_ICON[str(d.decision)] || 'ring', 'Gate', `${str(d.decision)} for ${str(d.delivery_id)}${after}`, { kind: 'delivery', id: str(d.delivery_id) });
        break;
      }
      case 'pull_request.open':
        if (e.outcome === 'ok') add(e, 'neutral', 'pr', 'Hub', `opens pull request #${num(d.number)} with the evidence report`, { kind: 'delivery', id: str(d.delivery_id) });
        break;
      case 'inspection.record':
        if (e.outcome === 'ok') add(e, str(d.verdict) === 'conforms' ? 'good' : 'critical', 'eye', who, `inspects ${str(d.object_id)}: ${str(d.verdict).replace(/_/g, ' ')}`, { kind: 'delivery', id: str(d.delivery_id) });
        break;
      case 'delivery.review':
        if (e.outcome === 'ok') add(e, VERDICT_STATUS[str(d.verdict)] || 'neutral', 'gavel', who, `verdict on ${str(d.delivery_id)}: ${(VERDICT_WORDS[str(d.verdict)] || str(d.verdict)).toLowerCase()}`, { kind: 'delivery', id: str(d.delivery_id) });
        break;
      default:
        break;
    }
  }
  flushPuts();
  for (const b of m.bursts) {
    const first = b.items[0].e;
    const count = b.items.length;
    const what = b.detail || b.reason;
    out.push({ ms: b.endMs, id: first.id, actor: b.actor, status: 'critical', icon: 'ban', who: 'Hub',
      text: `refuses ${count === 1 ? 'a call' : `${count} calls`} of the ${roleName(b.actor)}: ${what}`, open: { kind: 'refusals' } });
  }
  for (const turn of m.turns) {
    for (const call of turn.modelErrors) {
      out.push({ ms: call.ms, id: call.id, actor: turn.side, status: 'serious', icon: 'alert', who: 'Gateway',
        text: `a model call of the ${roleName(turn.side)} fails: ${str(call.data.detail) || 'provider error'}`, open: { kind: 'turn', key: turn.key } });
    }
  }
  out.sort((a, b) => a.ms - b.ms || a.id - b.id);
  return out;
}

// ------------------------------------------------------------------------- state at an instant
export function stateAt(m, ms) {
  const sides = {};
  for (const side of SIDES) {
    const turns = m.turns.filter((t) => t.side === side && t.start <= ms);
    const running = turns.find((t) => t.end === null || t.end > ms) || null;
    let calls = 0, errors = 0, tin = 0, tout = 0, cost = 0;
    let turnCalls = 0, turnCost = 0;
    for (const c of m.calls) {
      if (c.actor !== side || c.ms > ms) continue;
      if (c.outcome !== 'ok') { errors += 1; continue; }
      calls += 1; tin += num(c.data.prompt_tokens); tout += num(c.data.completion_tokens); cost += num(c.data.cost_usd);
      if (running && c.ms >= running.start) { turnCalls += 1; turnCost += num(c.data.cost_usd); }
    }
    const done = turns.filter((t) => t.end !== null && t.end <= ms);
    let last = null;
    for (const e of m.lastActions[side]) { if (e.ms <= ms) last = e; else break; }
    sides[side] = {
      turns, running, calls, errors, tin, tout, cost, turnCalls, turnCost, last,
      completed: done.filter((t) => !t.failed).length, failed: done.filter((t) => t.failed).length,
      refusals: m.refusals.filter((r) => r.actor === side && r.ms <= ms).length,
      lastEnd: done.length ? done[done.length - 1] : null,
    };
  }
  const version = versionAt(m, ms);
  let delivery = null;
  for (const d of m.deliveries) {
    const begun = (d.pushed || d.declared).ms;
    if (begun <= ms) delivery = d;
  }
  let decisionIndex = -1;
  if (delivery) delivery.decisions.forEach((d, i) => { if (d.ms <= ms) decisionIndex = i; });
  const decision = delivery && decisionIndex >= 0 ? delivery.decisions[decisionIndex] : null;
  const inspections = delivery ? delivery.inspections.filter((r) => r.ms <= ms) : [];
  const objects = judgedObjects(decision ? (m.versions.find((v) => v.number === decision.contractVersion) || version) : version);
  const judged = judge(objects, decision, inspections);
  const review = delivery ? delivery.reviews.filter((r) => r.ms <= ms).pop() || null : null;
  const verifying = Boolean(delivery && !decision && delivery.declared && delivery.declared.ms <= ms);
  return {
    ms, sides, version, delivery, decision, decisionIndex, judged, inspections, review, verifying,
    refusals: m.refusals.filter((r) => r.ms <= ms).length,
    moments: m.moments.filter((x) => x.ms <= ms),
    totals: {
      calls: sides.owner.calls + sides.contractor.calls,
      tin: sides.owner.tin + sides.contractor.tin,
      tout: sides.owner.tout + sides.contractor.tout,
      cost: sides.owner.cost + sides.contractor.cost,
    },
  };
}

