// The overview: what a viewer sees first. The gate's decision and the Owner's verdict, the two
// sides at work, what the run costs, the contract's requirements and the key moments.

import { h, icon, chip, replace } from './dom.js';
import { elapsed, duration, tokens, usd, int, plural, SIDE_NAME, VERDICT_WORDS } from './format.js';
import { DECISION_STATUS, DECISION_ICON, VERDICT_STATUS, STATE_LOOK } from './model.js';

const $ = (id) => document.getElementById(id);

export function renderHeader(m, mode) {
  const run = m.run;
  const project = run.project || {};
  replace($('project-name'), project.name || run.title || 'Project');
  const v = m.versions[m.versions.length - 1];
  const judged = v ? v.objects.filter((o) => o.type === 'requirement' || o.type === 'constraint').length : 0;
  replace($('project-sub'), [
    run.title || '',
    run.model ? `model ${run.model}, both sides` : null,
    judged ? `${judged} requirements and constraints` : null,
  ].filter(Boolean).join(' · '));

  const badges = [];
  if (run.kind === 'scripted') {
    badges.push(h('span', { class: 'badge kind-scripted' }, icon('shield'), `SCRIPTED ATTACK${(run.attacks || []).length ? ' · ' + run.attacks.join(', ') : ''}`));
  } else {
    badges.push(h('span', { class: 'badge kind-real', title: 'Real models behind the gateway: a run that actually happened' }, 'REAL RUN'));
  }
  const integrity = m.integrity || {};
  if (integrity.chain === 'verified') {
    const masked = (run.redactions || []).length > 0;
    badges.push(h('span', {
      class: 'badge',
      title: masked
        ? `The ${integrity.records} log records matched their hash chain when they were exported. This sample was masked afterwards (${run.redactions.join(', ')}), so its text no longer matches the hashes.`
        : `The ${integrity.records} exported log records match their hash chain.`,
    }, h('span', { class: 'good' }, icon('chain')), `Log chain verified · ${int(integrity.records)} records`));
  } else if (integrity.chain === 'broken') {
    badges.push(h('span', { class: 'badge', title: integrity.detail || '' }, h('span', { class: 'serious' }, icon('alert')), 'Log chain broken'));
  }
  if (mode === 'live') {
    badges.push(h('span', { class: 'badge live-badge' }, h('span', { class: 'live-dot' }), 'LIVE'));
  } else {
    badges.push(h('span', { class: 'badge' }, icon('clock'), 'REPLAY'));
  }
  replace($('badges'), badges);
}

// ------------------------------------------------------------------------------------ gate
export function renderGate(m, st, open) {
  const card = $('gate-card');
  const d = st.delivery;
  const decision = st.decision;
  card.className = `card gate-card clickable${decision ? ' status-' + (DECISION_STATUS[decision.decision] || 'neutral') : ''}`;
  const head = h('div', { class: 'card-head' },
    h('span', { class: 'eyebrow' }, d ? `Gate · delivery ${d.id}` : 'Gate'),
    d ? h('button', { class: 'more', type: 'button', on: { click: (ev) => { ev.stopPropagation(); open({ kind: 'delivery', id: d.id }); } } }, 'Evidence', icon('chevron')) : null);

  let main;
  if (!d) {
    const tests = new Set(m.events.filter((e) => e.type === 'acceptance.put' && e.outcome === 'ok' && e.ms <= st.ms).map((e) => e.data.path));
    main = h('div', { class: 'gate-main' },
      h('span', { class: 'decision none' }, 'No delivery yet'),
      h('div', { class: 'gate-facts' },
        h('span', null, h('b', null, plural(tests.size, 'acceptance test file')), ' from the Owner'),
        h('span', null, h('b', null, String(st.judged.length)), ' requirements and constraints to judge'),
        h('span', null, 'Rules: every P0 · 90 % of P1 · 80 %')));
  } else if (!decision) {
    main = h('div', { class: 'gate-main' },
      h('span', { class: 'decision pending' }, icon('spinner'), st.verifying ? 'Verifying…' : 'Received'),
      h('div', { class: 'gate-facts' },
        h('span', null, h('b', null, plural(d.changedPaths.length, 'changed path')), ` on ${d.branch}`),
        h('span', null, 'The hub replays the Owner\'s acceptance tests in a sandbox with no network.')));
  } else {
    const status = DECISION_STATUS[decision.decision] || 'neutral';
    const outcomes = Object.values(decision.outcomes);
    const passed = outcomes.filter((o) => o === 'passed').length;
    const verified = st.judged.filter((j) => j.state === 'verified').length;
    const awaiting = st.judged.filter((j) => j.state === 'awaiting');
    const why = awaiting.length
      ? `${awaiting.map((j) => j.id).join(', ')} ${awaiting.length === 1 ? 'awaits' : 'await'} inspection`
      : decision.after === 'inspection' ? 'after the Owner\'s inspections' : 'from the recorded results';
    const long = decision.decision.length > 5 ? ' long' : '';
    main = h('div', { class: 'gate-main' },
      h('span', { class: `decision ${status}${long}`, role: 'img', 'aria-label': `Decision ${decision.decision}` }, icon(DECISION_ICON[decision.decision] || 'ring'), decision.decision),
      h('div', { class: 'gate-facts' },
        h('span', null, h('b', null, `${passed} of ${outcomes.length}`), ' acceptance tests passed'),
        h('span', null, h('b', null, `${verified} of ${st.judged.length}`), ' requirements verified'),
        h('span', null, why)));
  }

  const trail = d && d.decisions.length ? h('div', { class: 'trail', 'aria-label': 'Decisions so far' },
    d.decisions.filter((x) => x.ms <= st.ms).map((x, i) => [
      i ? icon('arrow') : null,
      h('span', { class: `chip sm ${DECISION_STATUS[x.decision] || 'neutral'}` }, icon(DECISION_ICON[x.decision] || 'ring'), x.decision, h('span', { class: 'when' }, elapsed(x.ms))),
    ])) : null;
  if (trail) for (const a of trail.querySelectorAll(':scope > svg')) a.classList.add('arrow');

  const review = st.review;
  const verdict = h('div', { class: 'verdict-line' },
    h('span', null, 'Owner\'s verdict'),
    review
      ? chip(VERDICT_STATUS[review.verdict] || 'neutral', 'gavel', VERDICT_WORDS[review.verdict] || review.verdict)
      : h('span', { class: 'none' }, d && decision ? 'not recorded yet' : 'after a decision'),
    review ? h('span', { class: 'when' }, elapsed(review.ms)) : null);

  replace(card, head, main, trail, verdict);
}

// ----------------------------------------------------------------------------------- sides
export function renderSide(m, st, side, open) {
  const card = $(`side-${side}`);
  const s = st.sides[side];
  const name = SIDE_NAME[side];
  const what = side === 'owner' ? 'client side · writes the contract and the tests, judges' : 'builder side · designs, builds, delivers';
  const state = s.running
    ? h('span', { class: 'state running' }, h('span', { class: 'dot' }), `Running · ${duration(st.ms - s.running.start)}`)
    : h('span', { class: 'state' }, h('span', { class: 'dot' }), s.turns.length ? 'Idle' : 'Not started');
  const turnsWord = s.turns.length
    ? [`${s.turns.length}`, s.completed || s.failed ? ` · ${s.completed} completed${s.failed ? `, ${s.failed} failed` : ''}` : '']
    : ['none yet'];
  const facts = h('div', { class: 'facts' },
    h('span', null, 'Turns'), h('span', null, h('b', null, turnsWord[0]), turnsWord[1] || ''),
    h('span', null, 'Model calls'), h('span', null, h('b', null, int(s.calls)), ` · ${tokens(s.tin)} in · ${tokens(s.tout)} out`),
    s.running ? h('span', null, 'This turn') : h('span', null, 'Refused'),
    s.running
      ? h('span', null, h('b', null, plural(s.turnCalls, 'call')), ` · ${usd(s.turnCost)}`)
      : h('span', null, h('button', {
        class: `refused-btn${s.refusals ? '' : ' none'}`, type: 'button',
        on: { click: (ev) => { ev.stopPropagation(); open({ kind: 'refusals' }); } },
      }, icon('ban'), plural(s.refusals, 'call'))));
  const last = s.last;
  const foot = h('div', { class: 'side-foot' },
    h('span', null, 'Last'),
    h('span', { class: 'last' }, last ? lastWords(last) : '—'),
    last ? h('span', { class: 'when' }, elapsed(last.ms)) : null);
  replace(card,
    h('span', { class: `role-bar ${side}` }),
    h('div', { class: 'side-title' }, h('span', { class: 'name' }, name), state),
    h('div', { class: 'project-sub' }, what),
    h('div', { class: 'cost-big' }, usd(s.cost), h('small', null, s.running ? 'so far' : 'model cost')),
    facts, foot);
  card.onclick = () => {
    const turn = s.running || s.turns[s.turns.length - 1];
    if (turn) open({ kind: 'turn', key: turn.key });
  };
  card.classList.toggle('clickable', s.turns.length > 0);
}

/** What a side did, in words, from the hub's record of it. */
export function lastWords(e) {
  const d = e.data;
  if (e.outcome === 'refused') return `Refused: ${d.detail || d.reason || e.type}`;
  switch (e.type) {
    case 'delivery.review': return `Records its verdict on ${d.delivery_id}: ${(VERDICT_WORDS[d.verdict] || d.verdict || '').toLowerCase()}`;
    case 'inspection.record': return `Inspects ${d.object_id} on ${d.delivery_id}: ${String(d.verdict || '').replace(/_/g, ' ')}`;
    case 'acceptance.put': return `Stores the acceptance test ${d.path}`;
    case 'contract.read': return `Reads contract v${d.version}`;
    case 'contract.pending': return `Reads the pending changes (${d.pending})`;
    case 'contract.propose': return `Proposes change ${d.change_id}`;
    case 'staging.read': return d.delivery_id ? `Reads delivery ${d.delivery_id} from the staging area` : 'Reads the base from the staging area';
    case 'delivery.push': return `Pushes ${d.delivery_id} to the staging area`;
    case 'delivery.declare': return `Declares delivery ${d.delivery_id}`;
    case 'delivery.status': return `Reads the state of ${d.delivery_id}`;
    default: {
      const text = e.summary.replace(/^(Owner|Contractor)\s+/, '');
      return text.charAt(0).toUpperCase() + text.slice(1);
    }
  }
}

// ------------------------------------------------------------------------------------- run
export function renderRun(m, st, mode) {
  const card = $('run-card');
  const t = st.totals;
  const own = st.sides.owner.cost;
  const con = st.sides.contractor.cost;
  const total = own + con || 1;
  const seg = (side, value) => {
    const node = h('span', { class: `seg ${side}`, title: `${SIDE_NAME[side]}: ${usd(value)}` });
    node.style.flex = String(Math.max(value / total, 0.0001));
    return node;
  };
  const changes = m.changes.filter((c) => c.ms <= st.ms);
  const proposals = changes.filter((c) => c.kind === 'proposal');
  const versions = m.versions.filter((v) => v.ms <= st.ms);
  replace(card,
    h('div', { class: 'card-head' }, h('span', { class: 'eyebrow' }, mode === 'live' ? 'Run so far' : 'Run')),
    h('div', { class: 'cost-big' }, usd(t.cost), h('small', null, 'in all')),
    h('div', { class: 'split', role: 'img', 'aria-label': `Owner ${usd(own)}, Contractor ${usd(con)}` }, seg('owner', own), seg('contractor', con)),
    h('div', { class: 'split-legend' },
      h('span', null, h('span', { class: 'role-dot owner' }), 'Owner ', h('b', null, usd(own))),
      h('span', null, h('span', { class: 'role-dot contractor' }), 'Contractor ', h('b', null, usd(con)))),
    h('div', { class: 'run-facts' },
      h('span', null, h('b', null, duration(st.ms)), mode === 'live' || st.ms < m.durationMs ? ' since the first record' : ' from first to last record'),
      h('span', null, h('b', null, int(t.calls)), ` model calls · ${tokens(t.tin)} in · ${tokens(t.tout)} out`),
      h('span', null, 'Contract ', h('b', null, versions.length ? versions.map((v) => `v${v.number}`).join(' → ') : '—'),
        proposals.length ? ` · ${plural(proposals.length, 'change')} proposed` : '')));
}

// ---------------------------------------------------------------------------- requirements
export function renderRequirements(m, st, open) {
  const card = $('req-card');
  const verified = st.judged.filter((j) => j.state === 'verified').length;
  const head = h('div', { class: 'card-head' },
    h('span', { class: 'eyebrow' }, 'Contract ', st.version ? h('span', { class: 'as-is' }, `v${st.version.number}`) : null, ' · requirements and constraints'),
    h('button', { class: 'more', type: 'button', on: { click: () => open({ kind: 'contract' }) } },
      st.decision ? `${verified} of ${st.judged.length} verified` : 'Not judged yet', icon('chevron')));
  const tiles = st.judged.map((j) => {
    const look = STATE_LOOK[j.state];
    const how = j.state === 'verified' ? (j.inspection ? 'Inspected' : 'Tests pass') : j.state === 'awaiting' ? 'To inspect' : look.word;
    return h('button', {
      class: `req ${look.status}`, type: 'button',
      'aria-label': `${j.id}, ${j.priority}, verified by ${j.methods.join(' and ')}: ${how}`,
      on: { click: () => open({ kind: 'requirement', id: j.id }) },
    },
    h('span', { class: 'req-top' },
      h('span', { class: 'req-id' }, j.id),
      h('span', { class: `prio ${j.priority}` }, j.priority),
      h('span', { class: 'req-method' }, icon(j.methods.includes('test') ? 'flask' : 'eye'), j.methods.join(', ')),
      h('span', { class: 'req-status' }, icon(look.icon), how)),
    h('span', { class: 'req-name' }, j.name));
  });
  replace(card, head, h('div', { class: 'req-grid' }, tiles.length ? tiles : h('div', { class: 'empty' }, 'No requirement yet.')));
}

// ----------------------------------------------------------------------------- key moments
let shownMoments = new Set();

export function renderMoments(m, st, open, rows = 10) {
  const card = $('moments-card');
  const recent = st.moments.slice(-rows).reverse();
  const items = recent.map((x) => {
    const key = `${x.id}-${x.ms}`;
    const fresh = shownMoments.size > 0 && !shownMoments.has(key);
    return h('li', {
      class: `moment ${x.status}${fresh ? ' fresh' : ''}`, tabindex: x.open ? 0 : null, role: x.open ? 'button' : null,
      title: x.detail || null,
      on: x.open ? { click: () => open(x.open), keydown: (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); open(x.open); } } } : {},
    },
    h('span', { class: 'when' }, elapsed(x.ms)),
    h('span', { class: 'icon' }, icon(x.icon)),
    h('span', { class: 'what' }, h('span', { class: `who ${x.actor}` }, x.who), ' ', h('span', { class: 'detail' }, x.text)));
  });
  shownMoments = new Set(recent.map((x) => `${x.id}-${x.ms}`));
  const listNode = items.length ? h('ol', { class: 'moments', 'aria-label': 'Most recent first' }, items) : h('div', { class: 'empty' }, 'Nothing yet.');
  replace(card,
    h('div', { class: 'card-head' }, h('span', { class: 'eyebrow' }, 'Key moments'),
      h('button', { class: 'more', type: 'button', on: { click: () => open({ kind: 'moments' }) } }, `All ${st.moments.length}`, icon('chevron'))),
    listNode);
  // Keep the rows that fit whole: a clipped row reads as a mistake on a video.
  const limit = listNode.getBoundingClientRect().bottom;
  for (const li of [...listNode.children]) if (li.getBoundingClientRect().bottom > limit + 1) li.remove();
}
