// Details on demand: a drawer over the overview, for a delivery and its evidence, a turn, the
// contract, the refusals, one requirement, or every key moment. Each shows the state as of the
// playhead. Text written by an agent is shown as quoted speech, never styled like a hub fact.

import { h, s, icon, chip, replace, safeUrl } from './dom.js';
import { elapsed, duration, tokens, usd, int, plural, testWords, capitalize, roleName, SIDE_NAME, VERDICT_WORDS } from './format.js';
import {
  DECISION_STATUS, DECISION_ICON, VERDICT_STATUS, STATE_LOOK, FAMILIES,
  judgedObjects, judge, rulesOf, inspectionsUpTo, versionAt,
} from './model.js';

const PROTECTED = ['CONTRACT.md'];
const SHORT = { verified: 'yes', awaiting: 'awaits', failed: 'no', unknown: '?', pending: '—' };

export function createDrawers(m, { getState }) {
  const drawer = document.getElementById('drawer');
  const scrim = document.getElementById('scrim');
  let current = null;
  let returnFocus = null;

  function open(spec) {
    if (!current) returnFocus = document.activeElement;
    current = spec;
    render(true);
    drawer.hidden = false;
    scrim.hidden = false;
    const title = drawer.querySelector('h2');
    if (title) title.focus({ preventScroll: true });
  }

  function close() {
    if (!current) return;
    current = null;
    drawer.hidden = true;
    scrim.hidden = true;
    if (returnFocus && document.contains(returnFocus)) returnFocus.focus({ preventScroll: true });
  }

  function render(fresh) {
    const st = getState();
    const oldBody = drawer.querySelector('.drawer-body');
    const scroll = !fresh && oldBody ? oldBody.scrollTop : 0;
    const builders = { delivery, turn, contract, refusals, requirement, moments };
    const build = builders[current.kind] || moments;
    const [title, sub, chips, body] = build(st, current);
    const head = h('div', { class: 'drawer-head' },
      h('div', null,
        h('h2', { id: 'drawer-title', tabindex: -1 }, title),
        sub ? h('div', { class: 'sub' }, sub) : null,
        chips && chips.length ? h('div', { class: 'chips' }, chips) : null),
      h('button', { class: 'drawer-close', type: 'button', 'aria-label': 'Close', on: { click: close } }, icon('close')));
    const bodyNode = h('div', { class: 'drawer-body' }, body,
      h('p', { class: 'note' }, `As of ${elapsed(st.ms)} in the run.`));
    replace(drawer, head, bodyNode);
    bodyNode.scrollTop = scroll;
  }

  document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && current) close(); });
  scrim.addEventListener('click', close);

  // ------------------------------------------------------------------------------ delivery
  function delivery(st, spec) {
    const d = m.deliveries.find((x) => x.id === spec.id);
    if (!d) return ['Delivery', 'Not found in this run.', [], []];
    if ((d.pushed || d.declared).ms > st.ms) return [`Delivery ${d.id}`, 'Not delivered yet at this point of the run.', [], []];
    const decisions = d.decisions.filter((x) => x.ms <= st.ms);
    const latest = decisions[decisions.length - 1] || null;
    const review = d.reviews.filter((r) => r.ms <= st.ms).pop() || null;
    const pr = d.pr && d.pr.ms <= st.ms ? d.pr : null;
    const chips = [
      latest ? chip(DECISION_STATUS[latest.decision] || 'neutral', DECISION_ICON[latest.decision] || 'ring', `Gate: ${latest.decision}`) : chip('neutral', 'ring', 'Not decided yet'),
      review ? chip(VERDICT_STATUS[review.verdict] || 'neutral', 'gavel', `Owner: ${VERDICT_WORDS[review.verdict] || review.verdict}`) : null,
    ];
    const sub = ['Branch ', h('span', { class: 'inert' }, d.branch), ` · ${plural(d.changedPaths.length, 'changed path')}`,
      latest ? ` · judged against contract v${latest.contractVersion}` : '',
      pr ? [' · ', prLink(pr, (m.run.redactions || []).includes('renamed names'))] : ''];

    const body = [];
    body.push(section('How the decision was reached', null, stepsOf(d, st)));
    if (decisions.length) {
      const objects = judgedObjects(m.versions.find((v) => v.number === latest.contractVersion) || versionAt(m, st.ms));
      const columns = decisions.map((dec, i) => judge(objects, dec, inspectionsUpTo(d, i)));
      body.push(section('Requirements at each decision', null, matrix(objects, decisions, columns)));
      body.push(section('Gate rules at the latest decision', null, rules(columns[columns.length - 1])));
      body.push(section('Acceptance tests of the Owner', String(Object.keys(latest.outcomes).length), testsList(latest.outcomes)));
    }
    const inspections = d.inspections.filter((r) => r.ms <= st.ms);
    if (inspections.length) {
      body.push(section('Inspections', String(inspections.length), inspections.map((r) => quote('owner',
        [`inspection of ${r.object}`, r.verdict.replace(/_/g, ' ')], r.ms, r.observation))));
    }
    if (review) body.push(section('The Owner\'s verdict', null, quote('owner', [VERDICT_WORDS[review.verdict] || review.verdict], review.ms, review.reasons, true)));
    body.push(section('Changed paths', String(d.changedPaths.length), h('ul', { class: 'list cols-2' }, d.changedPaths.map((p) => {
      const flagged = PROTECTED.includes(p);
      return h('li', { class: flagged ? 'warning' : '' }, flagged ? icon('alert') : icon('doc'), h('span', { class: 'main mono' }, p),
        h('span', { class: 'meta' }, flagged ? 'protected path: flagged' : ''));
    }))));
    if (decisions.length) {
      body.push(h('p', { class: 'note' }, 'In this mockup the table by requirement is approximated from the replay (each test\'s outcome and the inspections). The dashboard takes it from the gate itself, which computes it from the recorded results.'));
    }
    return [`Delivery ${d.id}`, sub, chips, body];
  }

  function stepsOf(d, st) {
    const steps = [];
    const at = (ms) => ms <= st.ms;
    if (d.pushed && at(d.pushed.ms)) steps.push(step(d.pushed.ms, 'neutral', 'box', `The Contractor pushes ${d.id} to the staging area`, `${plural(d.changedPaths.length, 'changed path')} on its branch, as a git bundle.`));
    if (d.declared && at(d.declared.ms)) steps.push(step(d.declared.ms, 'neutral', 'box', `The Contractor declares ${d.id}`, 'From here the hub decides. The Contractor never reports a result.'));
    if (d.verification && at(d.verification.ms)) {
      const v = d.verification.data;
      const ok = d.verification.outcome === 'ok' && v.passed === v.tests;
      steps.push(step(d.verification.ms, ok ? 'good' : 'critical', 'flask',
        d.verification.outcome === 'ok' ? `The hub replays the Owner's ${v.tests} acceptance tests: ${v.passed} pass` : 'The hub cannot run the verification',
        'In a sandbox with no network. Two canary tests of the hub must fail, or the run is not believed.'));
    }
    d.decisions.forEach((dec, i) => {
      if (!at(dec.ms)) return;
      for (const r of dec.inspections) {
        if (!at(r.ms)) continue;
        // The hub logs the new decision just before the inspection that caused it: tell it in
        // the order it happened.
        const x = step(r.ms, r.verdict === 'conforms' ? 'good' : 'critical', 'eye', `The Owner inspects ${r.object}: ${r.verdict.replace(/_/g, ' ')}`, 'What tests cannot show is judged by an inspection the Owner records.');
        x.ms = Math.min(r.ms, dec.ms) - 0.5;
        steps.push(x);
      }
      const objects = judgedObjects(m.versions.find((v) => v.number === dec.contractVersion) || versionAt(m, dec.ms));
      const judged = judge(objects, dec, inspectionsUpTo(d, i));
      const awaiting = judged.filter((j) => j.state === 'awaiting');
      const why = awaiting.length
        ? `${awaiting.map((j) => `${j.id} (${j.priority})`).join(' and ')} ${awaiting.length === 1 ? 'awaits' : 'await'} an inspection: a reservation, so not PASS.`
        : dec.decision === 'PASS' ? 'Every rule is met: all P0, 90 % of P1, 80 % overall.' : 'The rules are not met.';
      steps.push(step(dec.ms, DECISION_STATUS[dec.decision] || 'neutral', DECISION_ICON[dec.decision] || 'ring',
        i === 0 ? `Gate: ${dec.decision}` : `The gate decides again: ${dec.decision}`, why, dec.previous ? `was ${dec.previous}` : ''));
    });
    if (d.pr && at(d.pr.ms)) steps.push(step(d.pr.ms, 'neutral', 'pr', `The hub opens pull request #${d.pr.number}`, 'With the evidence report: base, head, contract and tests by digest, every outcome, the rules, the cost.'));
    const refused = d.refusedReviews.filter((e) => at(e.ms));
    if (refused.length) {
      steps.push(step(refused[refused.length - 1].ms, 'critical', 'ban',
        `The hub refuses ${refused.length === 1 ? 'a verdict' : `${refused.length} verdicts`} of the Owner`, refused[0].data.detail || refused[0].data.reason || ''));
    }
    for (const r of d.reviews) {
      if (!at(r.ms)) continue;
      steps.push(step(r.ms, VERDICT_STATUS[r.verdict] || 'neutral', 'gavel', `The Owner's verdict: ${(VERDICT_WORDS[r.verdict] || r.verdict).toLowerCase()}`, 'Posted on the pull request. The merge stays a human decision.'));
    }
    steps.sort((a, b) => a.ms - b.ms);
    return steps.length ? h('ol', { class: 'steps' }, steps.map((x) => x.node)) : h('div', { class: 'empty' }, 'Nothing yet.');
  }

  function step(ms, status, iconName, title, detail, extra) {
    return { ms, node: h('li', { class: `step ${status}` },
      h('span', { class: 'bullet' }, icon(iconName)),
      h('div', null, h('div', { class: 'title' }, title), detail ? h('div', { class: 'detail' }, detail) : null),
      h('span', { class: 'when' }, elapsed(ms), extra ? ` · ${extra}` : '')) };
  }

  function matrix(objects, decisions, columns) {
    const head = h('tr', null, h('th', { scope: 'col' }, 'Requirement'), h('th', { scope: 'col' }, 'Priority'), h('th', { scope: 'col' }, 'Method'),
      decisions.map((dec, i) => h('th', { scope: 'col', class: 'c' }, h('span', { class: 'colhead' },
        h('span', { class: `cell ${DECISION_STATUS[dec.decision]}` }, icon(DECISION_ICON[dec.decision]), `#${i + 1} ${dec.decision}`),
        h('span', { class: 'when' }, elapsed(dec.ms))))));
    const rows = objects.map((o, r) => h('tr', { class: 'clickable', on: { click: () => open({ kind: 'requirement', id: o.id }) } },
      h('td', null, h('span', { class: 'mono' }, o.id), ' ', h('span', { class: 'asof' }, o.name)),
      h('td', null, h('span', { class: `prio ${o.priority}` }, o.priority)),
      h('td', null, o.methods.join(', ')),
      columns.map((col) => {
        const j = col[r];
        const look = STATE_LOOK[j.state];
        return h('td', { class: 'c' }, h('span', { class: `cell ${look.status}`, title: look.word }, icon(look.icon), SHORT[j.state]));
      })));
    return h('table', { class: 'grid' }, h('thead', null, head), h('tbody', null, rows));
  }

  function rules(judged) {
    return h('div', { class: 'rules' }, rulesOf(judged).map((r) => {
      const share = r.total ? (r.verified / r.total) * 100 : 100;
      const withAwaiting = r.total ? ((r.verified + r.awaiting) / r.total) * 100 : 100;
      const met = share >= r.required;
      const once = !met && withAwaiting >= r.required;
      const bar = h('div', { class: 'bar', role: 'img', 'aria-label': `${r.verified} of ${r.total} verified, ${r.awaiting} awaiting, threshold ${r.required} %` });
      if (r.verified) { const f = h('span', { class: 'fill' }); f.style.width = `${share}%`; bar.append(f); }
      if (r.awaiting) { const f = h('span', { class: 'fill awaiting' }); f.style.width = `${(r.awaiting / r.total) * 100}%`; bar.append(f); }
      const t = h('span', { class: 'threshold' }); t.style.left = `calc(${r.required}% - 1.5px)`; bar.append(t);
      const verdict = met ? h('span', { class: 'verdict good' }, icon('checkCircle'), 'met')
        : once ? h('span', { class: 'verdict warning' }, icon('half'), 'met once inspected')
          : h('span', { class: 'verdict critical' }, icon('crossCircle'), 'not met');
      return h('div', { class: 'rule' },
        h('span', { class: 'label' }, `${r.name}: ${r.verified} of ${r.total}`, h('small', null, r.sub)),
        bar, verdict);
    }));
  }

  function testsList(outcomes) {
    const rows = Object.entries(outcomes).map(([id, outcome]) => {
      const [file] = String(id).split('::');
      const ok = outcome === 'passed';
      return h('li', { class: ok ? 'good' : 'critical' }, icon(ok ? 'check' : 'cross'),
        h('span', { class: 'main' }, capitalize(testWords(id)), h('small', { class: 'mono' }, file)),
        h('span', { class: 'meta' }, outcome));
    });
    return h('ul', { class: 'list' }, rows);
  }

  // ---------------------------------------------------------------------------------- turn
  function turn(st, spec) {
    const t = m.turns.find((x) => x.key === spec.key) || pickTurn(spec, st);
    if (!t) return ['Turn', 'Not found in this run.', [], []];
    if (t.start > st.ms) return [`${SIDE_NAME[t.side]} · turn ${t.n}`, 'Not started yet at this point of the run.', [], []];
    const running = t.end === null || t.end > st.ms;
    const end = running ? st.ms : t.end;
    const calls = t.calls.filter((c) => c.ms <= st.ms);
    const ok = calls.filter((c) => c.outcome === 'ok');
    const tin = ok.reduce((a, c) => a + (c.data.prompt_tokens || 0), 0);
    const tout = ok.reduce((a, c) => a + (c.data.completion_tokens || 0), 0);
    const cost = ok.reduce((a, c) => a + (c.data.cost_usd || 0), 0);
    const refused = t.refusals.filter((e) => e.ms <= st.ms);
    const state = running ? chip('neutral', 'spinner', 'Running')
      : t.failed ? chip('serious', 'alert', `Failed · ${t.result}`) : chip('good', 'check', 'Completed');
    const chips = [state, chip('neutral', 'speech', plural(ok.length, 'model call')), chip('neutral', 'doc', `${tokens(tin)} in · ${tokens(tout)} out`),
      chip('neutral', 'key', usd(cost)), chip(refused.length ? 'critical' : 'neutral', 'ban', `${plural(refused.length, 'call')} refused`)];
    const sub = `${elapsed(t.start)} → ${running ? 'now' : elapsed(end)} · ${duration(end - t.start)} · started by the ${t.startedBy ? roleName(t.startedBy) : 'hub'} with a ${int(t.promptChars)}-character prompt`;
    const body = [];
    if (calls.length) body.push(section('Input tokens of each model call', String(calls.length), callsChart(t.side, calls)));
    body.push(section('What happened', null, conversation(t, st)));
    body.push(h('p', { class: 'note' }, 'The agents\' words come from their transcripts, recorded in the replay and not covered by the log\'s hash chain. The live dashboard shows the hub\'s own records of a turn; see ADR 0021.'));
    return [`${SIDE_NAME[t.side]} · turn ${t.n}`, `${t.label} · ${sub}`, chips, body];
  }

  function pickTurn(spec, st) {
    const turns = m.turns.filter((t) => (!spec.side || t.side === spec.side) && t.start <= (spec.at ?? st.ms));
    return turns[turns.length - 1] || null;
  }

  function callsChart(side, calls) {
    const W = 940, H = 190, left = 52, bottom = 24, top = 18;
    const max = Math.max(1, ...calls.map((c) => c.data.prompt_tokens || 0));
    const ceiling = Math.max(50000, Math.ceil(max / 50000) * 50000);
    const plotW = W - left - 8;
    const plotH = H - top - bottom;
    const bw = Math.max(2, plotW / calls.length - 2);
    const svg = s('svg', { class: 'calls-svg', viewBox: `0 0 ${W} ${H}`, role: 'img',
      'aria-label': `Input tokens per model call, from ${tokens(calls[0].data.prompt_tokens || 0)} to a maximum of ${tokens(max)}.` });
    for (let v = 0; v <= ceiling; v += 50000) {
      const y = top + plotH - (v / ceiling) * plotH;
      svg.append(s('line', { class: 'gl', x1: left, x2: W - 8, y1: y, y2: y }));
      svg.append(s('text', { class: 'axis', x: left - 8, y: y + 4, 'text-anchor': 'end' }, v ? `${v / 1000} k` : '0'));
    }
    let maxIndex = 0;
    calls.forEach((c, i) => {
      const v = c.data.prompt_tokens || 0;
      if (v > (calls[maxIndex].data.prompt_tokens || 0)) maxIndex = i;
      const hh = c.outcome === 'ok' ? Math.max(2, (v / ceiling) * plotH) : plotH;
      const x = left + i * (plotW / calls.length) + 1;
      const bar = s('rect', { class: `bar-in ${c.outcome === 'ok' ? side : 'error'}`, x, y: top + plotH - hh, width: bw, height: hh, rx: 2 });
      bar.append(s('title', null, c.outcome === 'ok'
        ? `Call ${i + 1} at ${elapsed(c.ms)}: ${int(v)} tokens in, ${int(c.data.completion_tokens || 0)} out, ${c.data.tool_calls || 0} tool calls`
        : `Call ${i + 1} at ${elapsed(c.ms)} failed: ${c.data.detail || 'error'}`));
      svg.append(bar);
    });
    const mx = left + maxIndex * (plotW / calls.length) + bw / 2;
    const my = top + plotH - ((calls[maxIndex].data.prompt_tokens || 0) / ceiling) * plotH;
    svg.append(s('text', { class: 'annot', x: Math.min(mx, W - 90), y: Math.max(top + 2, my - 8), 'text-anchor': 'middle' }, `${tokens(max)} tokens`));
    svg.append(s('text', { class: 'axis', x: left, y: H - 4 }, 'first call'));
    svg.append(s('text', { class: 'axis', x: W - 8, y: H - 4, 'text-anchor': 'end' }, 'last call'));
    return h('div', { class: 'chart-wrap' },
      h('div', { class: 'chart-title' }, 'Each call sends the conversation so far: a turn costs more the longer it runs'),
      h('div', { class: 'chart-sub' }, `Largest call: ${int(max)} tokens in. Failed calls are drawn full height in orange.`),
      svg);
  }

  function conversation(t, st) {
    const items = [];
    for (const e of t.messages) if (e.ms <= st.ms) items.push({ ms: e.ms, kind: 'say', e });
    for (const e of t.actions) if (e.ms <= st.ms) items.push({ ms: e.ms, kind: 'act', e });
    for (const e of t.refusals) if (e.ms <= st.ms) items.push({ ms: e.ms, kind: 'refused', e });
    for (const e of t.calls) if (e.ms <= st.ms) items.push({ ms: e.ms, kind: 'call', e });
    items.sort((a, b) => a.ms - b.ms);
    const rows = [];
    let calls = [];
    let refused = [];
    const flushCalls = () => {
      if (!calls.length) return;
      const ok = calls.filter((c) => c.outcome === 'ok');
      const errors = calls.length - ok.length;
      const tin = ok.reduce((a, c) => a + (c.data.prompt_tokens || 0), 0);
      rows.push(h('li', { class: `act${errors ? ' serious' : ''}` }, h('span', { class: 'when' }, elapsed(calls[0].ms)), icon(errors ? 'alert' : 'speech'),
        h('span', null, plural(calls.length, 'model call'), ` · ${tokens(tin)} tokens in`, errors ? ` · ${errors} failed: ${calls.find((c) => c.outcome !== 'ok').data.detail || 'error'}` : '')));
      calls = [];
    };
    const flushRefused = () => {
      if (!refused.length) return;
      const details = [...new Set(refused.map((e) => e.data.detail || e.data.reason))];
      rows.push(h('li', { class: 'act critical' }, h('span', { class: 'when' }, elapsed(refused[0].ms)), icon('ban'),
        h('span', null, h('b', null, `${plural(refused.length, 'call')} refused`), `: ${details.join('; ')}`)));
      refused = [];
    };
    for (const it of items) {
      if (it.kind === 'call') { flushRefused(); calls.push(it.e); continue; }
      if (it.kind === 'refused') { flushCalls(); refused.push(it.e); continue; }
      flushCalls(); flushRefused();
      if (it.kind === 'say') {
        const d = it.e.data;
        const tags = [d.role === 'final' ? 'final answer' : null, d.truncated ? 'shortened' : null].filter(Boolean);
        rows.push(h('li', { class: 'say' }, h('span', { class: 'when' }, elapsed(it.ms)), quote(t.side, tags, null, d.text || '', d.role === 'final')));
      } else {
        const e = it.e;
        const text = e.summary.replace(/^(Owner|Contractor)\s+/, '');
        const good = e.type === 'delivery.declare' || e.type === 'inspection.record' || e.type === 'delivery.review';
        rows.push(h('li', { class: `act${good ? ' good' : ''}` }, h('span', { class: 'when' }, elapsed(it.ms)), icon(actionIcon(e.type)),
          h('span', null, capitalize(text))));
      }
    }
    flushCalls(); flushRefused();
    return rows.length ? h('ol', { class: 'convo' }, rows) : h('div', { class: 'empty' }, 'Nothing yet.');
  }

  // ------------------------------------------------------------------------------ contract
  function contract(st) {
    const v = versionAt(m, st.ms);
    if (!v) return ['Contract', 'No version in this run.', [], []];
    const count = (type) => v.objects.filter((o) => o.type === type).length;
    const sub = `${plural(count('requirement'), 'requirement')} · ${plural(count('constraint'), 'constraint')} · ${plural(count('story'), 'story', 'stories')} · ${plural(count('criterion'), 'criterion', 'criteria')}`;
    const changes = m.changes.filter((c) => c.ms <= st.ms);
    const steps = [];
    for (const c of changes) {
      const flagged = Object.entries(c.flagged || {});
      const flagText = flagged.length ? ` Flagged: ${flagged.map(([id, terms]) => `"${[].concat(terms).join('", "')}" in ${id}`).join('; ')}.` : '';
      if (c.kind === 'proposal') {
        steps.push(step(c.ms, 'neutral', 'doc', `The ${roleName(c.by)} proposes change ${c.id}, from v${c.base}`, `${c.summary} (${plural(c.operations, 'operation')}).${flagText}`));
        if (c.resolvedMs !== undefined && c.resolvedMs <= st.ms) {
          steps.push(step(c.resolvedMs, c.status === 'approved' ? 'good' : 'critical', c.status === 'approved' ? 'check' : 'cross',
            `The ${roleName(c.resolvedBy)} ${c.status === 'approved' ? 'approves' : 'rejects'} change ${c.id}${c.version ? ` → v${c.version}` : ''}`,
            'A change is applied only once another principal approves it: no side approves its own.'));
        }
      } else {
        steps.push(step(c.ms, 'neutral', 'doc', `The ${roleName(c.by)} applies a reserved change → v${c.version ?? '?'}`,
          `${c.summary} (${plural(c.operations, 'operation')}).${flagText}`));
      }
    }
    steps.sort((a, b) => a.ms - b.ms);
    const latest = st.delivery && st.decision ? st.judged : judge(judgedObjects(v), null, []);
    const table = h('table', { class: 'grid' },
      h('thead', null, h('tr', null, ['ID', 'Priority', 'Method', 'Requirement', 'Covered by', 'State'].map((x) => h('th', { scope: 'col' }, x)))),
      h('tbody', null, latest.map((j) => {
        const look = STATE_LOOK[j.state];
        return h('tr', { class: 'clickable', on: { click: () => open({ kind: 'requirement', id: j.id }) } },
          h('td', null, h('span', { class: 'mono' }, j.id)),
          h('td', null, h('span', { class: `prio ${j.priority}` }, j.priority)),
          h('td', null, j.methods.join(', ')),
          h('td', null, j.text.length > 150 ? `${j.text.slice(0, 150)}…` : j.text),
          h('td', null, j.stories.map((x) => x.id).join(', ') || '—'),
          h('td', null, h('span', { class: `cell ${look.status}` }, icon(look.icon), look.word)));
      })));
    const stories = v.objects.filter((o) => o.type === 'story');
    const storyList = h('ul', { class: 'list' }, stories.map((st2) => {
      const criteria = v.objects.filter((c) => c.type === 'criterion' && String(c.id).startsWith(`${st2.id}.`));
      return h('li', null, icon('doc'), h('span', { class: 'main' }, h('span', { class: 'mono' }, st2.id), ` ${st2.title || ''}`,
        h('small', null, st2.text || '')), h('span', { class: 'meta' }, plural(criteria.length, 'criterion', 'criteria')));
    }));
    return [`Contract v${v.number}`, sub, [], [
      section('Changes in this run', String(changes.length), steps.length ? h('ol', { class: 'steps' }, steps.map((x) => x.node)) : h('div', { class: 'empty' }, 'No change yet.')),
      section('Requirements and constraints', String(latest.length), table),
      section('Stories', String(stories.length), storyList),
    ]];
  }

  // ------------------------------------------------------------------------------ refusals
  function refusals(st) {
    const list = m.refusals.filter((r) => r.ms <= st.ms);
    const byFamily = Object.fromEntries(FAMILIES.map((f) => [f.key, list.filter((r) => r.family === f.key).length]));
    const actors = [...new Set(list.map((r) => r.actor))];
    const sub = list.length
      ? `${plural(list.length, 'call')} refused so far · ${actors.map((a) => `${list.filter((r) => r.actor === a).length} by the ${roleName(a)}`).join(', ')}`
      : 'No call refused so far.';
    const tiles = h('div', { class: 'families' }, FAMILIES.map((f) => h('div', { class: `family${f.key === 'role' && byFamily.role ? ' hot' : ''}` },
      h('div', { class: `n${byFamily[f.key] ? '' : ' zero'}` }, int(byFamily[f.key])),
      h('div', { class: 'label' }, f.label), h('div', { class: 'hint' }, f.hint))));
    const details = [...new Set(list.map((r) => r.detail || r.reason))];
    const lede = !list.length ? 'Nothing was refused yet.'
      : byFamily.role ? `${plural(byFamily.role, 'call')} outside the caller's role refused: the hub logged each and did nothing it asked.`
        : `No call outside its role so far. The ${plural(list.length, 'refusal')} ${list.length === 1 ? 'is' : 'are'} mistakes the hub turned away: ${details.slice(0, 3).join('; ')}.`;
    const bursts = m.bursts.filter((b) => b.ms <= st.ms).map((b) => {
      const items = b.items.filter((r) => r.ms <= st.ms);
      const requested = [...new Set(items.map((r) => r.requested).filter(Boolean))];
      return h('li', { class: 'critical' }, icon('ban'),
        h('span', { class: 'main' }, h('b', null, `${plural(items.length, 'call')} of the ${roleName(b.actor)}`),
          ' · ', h('span', { class: 'inert' }, b.reason), ' · ', b.detail,
          requested.length ? h('small', null, 'asked for: ', requested.map((r) => h('span', { class: 'inert' }, r))) : null,
          h('small', null, `family: ${(FAMILIES.find((f) => f.key === b.family) || {}).label || b.family}`)),
        h('span', { class: 'meta' }, items.length > 1 ? `${elapsed(b.ms)}–${elapsed(items[items.length - 1].ms)}` : elapsed(b.ms)));
    });
    return ['Refused by the hub', sub, [], [
      h('div', { class: 'section' }, tiles, h('p', { class: 'lede' }, lede)),
      section('In bursts', String(bursts.length), bursts.length ? h('ul', { class: 'list' }, bursts) : h('div', { class: 'empty' }, 'None.')),
      h('p', { class: 'note' }, 'Every call, allowed or refused, leaves one record in the hash-chained log, with a stable reason code. A refused call changes nothing.'),
    ]];
  }

  // --------------------------------------------------------------------------- requirement
  function requirement(st, spec) {
    const v = versionAt(m, st.ms);
    const j = (st.decision ? st.judged : judge(judgedObjects(v), null, [])).find((x) => x.id === spec.id);
    if (!j) return [spec.id, 'Not in the contract at this point of the run.', [], []];
    const look = STATE_LOOK[j.state];
    const chips = [h('span', { class: `prio ${j.priority}` }, j.priority), chip('neutral', j.methods.includes('test') ? 'flask' : 'eye', `verified by ${j.methods.join(' and ')}`), chip(look.status, look.icon, look.word)];
    const body = [h('div', { class: 'section' }, h('div', { class: 'quote' }, j.text))];
    body.push(section('Covered by', j.stories.length ? String(j.stories.length) : null, j.stories.length ? h('ul', { class: 'list' }, j.stories.map((story) => h('li', null, icon('doc'),
      h('span', { class: 'main' }, h('span', { class: 'mono' }, story.id), ` ${story.title}`, story.criteria.map((c) => h('small', null, h('span', { class: 'mono' }, c.id), ` ${c.text}`))),
      h('span', { class: 'meta' }, plural(story.criteria.length, 'criterion', 'criteria'))))) : h('div', { class: 'empty' }, j.methods.includes('test') ? 'No story covers it yet: no test can verify it.' : 'A constraint: no story; an inspection verifies it.')));
    const d = st.delivery;
    if (d) {
      const rows = d.decisions.filter((x) => x.ms <= st.ms).map((dec, i) => {
        const objects = judgedObjects(m.versions.find((x) => x.number === dec.contractVersion) || v);
        const jj = judge(objects, dec, inspectionsUpTo(d, i)).find((x) => x.id === j.id);
        const lk = STATE_LOOK[jj ? jj.state : 'pending'];
        return h('li', { class: lk.status }, icon(lk.icon), h('span', { class: 'main' }, `Decision #${i + 1} on ${d.id}: ${dec.decision}`), h('span', { class: 'meta' }, `${lk.word} · ${elapsed(dec.ms)}`));
      });
      if (rows.length) body.push(section('At each decision', String(rows.length), h('ul', { class: 'list' }, rows)));
    }
    if (j.inspection) body.push(section('Inspection', null, quote('owner', [`inspection of ${j.id}`, j.inspection.verdict.replace(/_/g, ' ')], j.inspection.ms, j.inspection.observation)));
    return [`${j.id} · ${j.name}`, j.type === 'constraint' ? 'A constraint of the contract' : 'A requirement of the contract', chips, body];
  }

  // ------------------------------------------------------------------------------- moments
  function moments(st) {
    const items = st.moments.slice().reverse().map((x) => h('li', {
      class: `moment ${x.status}`, tabindex: x.open ? 0 : null, role: x.open ? 'button' : null,
      on: x.open ? { click: () => open(x.open), keydown: (ev) => { if (ev.key === 'Enter') open(x.open); } } : {},
    }, h('span', { class: 'when' }, elapsed(x.ms)), h('span', { class: 'icon' }, icon(x.icon)),
    h('span', { class: 'what' }, h('span', { class: 'who' }, x.who), ' ', h('span', { class: 'detail' }, x.text))));
    return ['Key moments', `${plural(st.moments.length, 'moment')} so far, most recent first. Model calls and reads are left out.`, [],
      [h('ol', { class: 'moments' }, items)]];
  }

  return { open, close, refresh: () => { if (current) render(false); }, isOpen: () => Boolean(current) };
}

// ---------------------------------------------------------------------------------- helpers
function section(title, count, content) {
  return h('section', { class: 'section' }, h('h3', null, title, count ? h('span', { class: 'count' }, count) : null), content);
}

function quote(side, tags, ms, text, clampable) {
  const node = h('div', { class: `quote ${side}${clampable && text.length > 600 ? ' clamped' : ''}` }, text);
  const head = h('div', { class: 'quote-head' }, h('span', { class: `role-dot ${side}` }), h('span', { class: 'who' }, SIDE_NAME[side] || side),
    tags.filter(Boolean).map((t) => h('span', { class: 'tag' }, t)), ms !== null && ms !== undefined ? h('span', { class: 'asof' }, elapsed(ms)) : null);
  const parts = [head, node];
  if (clampable && text.length > 600) {
    const more = h('button', { class: 'linklike', type: 'button', 'aria-expanded': 'false' }, 'Show all');
    more.addEventListener('click', () => {
      const expanded = node.classList.toggle('clamped') === false;
      more.textContent = expanded ? 'Show less' : 'Show all';
      more.setAttribute('aria-expanded', String(expanded));
    });
    parts.push(more);
  }
  return h('div', null, parts);
}

function prLink(pr, masked) {
  const url = masked ? null : safeUrl(pr.url);
  return url ? h('a', { href: url, target: '_blank' }, `pull request #${pr.number}`) : `pull request #${pr.number}${masked ? ' (address masked)' : ''}`;
}

function actionIcon(type) {
  if (type.startsWith('contract.')) return 'doc';
  if (type === 'acceptance.put') return 'flask';
  if (type === 'staging.read') return 'box';
  if (type.startsWith('delivery.')) return type === 'delivery.review' ? 'gavel' : 'box';
  if (type === 'inspection.record') return 'eye';
  return 'chevron';
}

