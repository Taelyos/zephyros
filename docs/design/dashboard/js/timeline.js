// The timeline: one lane per party, the run's elapsed time across. Turns are bars (their model
// calls a rug inside), refusals are red ticks above the bar, contract versions and gate decisions
// are markers. Everything is drawn as of the playhead: nothing after it exists yet.

import { s, clear } from './dom.js';
import { elapsed, duration, tokens, usd, plural, SIDE_NAME, VERDICT_WORDS } from './format.js';
import { DECISION_STATUS, VERDICT_STATUS } from './model.js';

const LANES = [
  { key: 'contract', label: 'Contract', h: 34 },
  { key: 'owner', label: 'Owner', h: 56 },
  { key: 'contractor', label: 'Contractor', h: 56 },
  { key: 'gate', label: 'Gate', h: 38 },
];
const LEFT = 118;
const RIGHT = 14;
const TOP = 22;
const AXIS = 24;
const GAP = 6;

export function createTimeline(svg, m, { open, tip }) {
  let last = null;

  function render(st, mode) {
    last = { st, mode };
    const box = svg.getBoundingClientRect();
    const W = Math.max(600, Math.round(box.width));
    const H = Math.max(180, Math.round(box.height));
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    clear(svg);

    const domain = mode === 'live' ? Math.max(st.ms * 1.08, 10 * 60000) : Math.max(m.durationMs, 1);
    const plotW = W - LEFT - RIGHT;
    const x = (ms) => LEFT + (Math.min(ms, domain) / domain) * plotW;
    // Lanes share the height there is: side lanes take what is left over, and all shrink
    // together when the card is short.
    const weights = LANES.reduce((a, l) => a + l.h, 0);
    const room = H - TOP - AXIS - GAP * (LANES.length - 1);
    const scale = Math.min(1, room / weights);
    const extra = Math.max(0, room - weights);
    const lanes = [];
    let y = TOP;
    for (const lane of LANES) {
      const hh = Math.floor(lane.h * scale) + (lane.key === 'owner' || lane.key === 'contractor' ? Math.floor(extra / 2) : 0);
      lanes.push({ ...lane, y, h: hh, mid: y + hh / 2 });
      y += hh + GAP;
    }
    const lane = Object.fromEntries(lanes.map((l) => [l.key, l]));
    const bottom = y - GAP;

    svg.append(s('defs', null,
      s('pattern', { id: 'hatch-failed', width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' },
        s('rect', { width: 6, height: 6, fill: '#3a2618' }),
        s('rect', { width: 2.5, height: 6, fill: '#f2843a' }))));

    // lanes and their names
    for (const l of lanes) {
      svg.append(s('rect', { class: 'tl-lane-bg', x: LEFT, y: l.y, width: plotW, height: l.h, rx: 6 }));
      svg.append(s('text', { class: 'tl-lane-label', x: 0, y: l.mid + 5 }, l.label));
    }

    // time axis
    const minutes = domain / 60000;
    const step = minutes <= 20 ? 2 : minutes <= 60 ? 5 : minutes <= 180 ? 10 : 30;
    for (let t = 0; t <= minutes + 0.001; t += step) {
      const xx = x(t * 60000);
      svg.append(s('line', { class: 'tl-gridline', x1: xx, x2: xx, y1: TOP, y2: bottom }));
      const label = t === 0 ? '0' : t >= 60 ? `${Math.floor(t / 60)} h${t % 60 ? ' ' + String(t % 60).padStart(2, '0') : ''}` : `${t} min`;
      svg.append(s('text', { class: 'tl-axis', x: xx, y: bottom + 18, 'text-anchor': t === 0 ? 'start' : 'middle' }, label));
    }

    drawContract(svg, m, st, lane.contract, x, open, tip);
    for (const side of ['owner', 'contractor']) drawSide(svg, m, st, side, lane[side], x, open, tip);
    drawGate(svg, m, st, lane.gate, x, open, tip, W - RIGHT);

    // playhead
    const px = x(st.ms);
    svg.append(s('g', { class: 'tl-playhead', 'aria-hidden': 'true' },
      s('line', { x1: px, x2: px, y1: TOP - 6, y2: bottom + 2 }),
      s('text', { class: 'tl-axis', x: Math.min(px, W - RIGHT - 30), y: 12, 'text-anchor': 'middle' }, mode === 'live' ? 'now' : elapsed(st.ms))));
    svg.setAttribute('aria-label', `Timeline of the run up to ${elapsed(st.ms)}: ${m.turns.filter((t) => t.start <= st.ms).length} turns, ${st.refusals} refused calls.`);
  }

  return { render };
}

function interactive(node, label, sub, onOpen, tip) {
  node.setAttribute('tabindex', '0');
  node.setAttribute('role', 'button');
  node.setAttribute('aria-label', sub ? `${label}. ${sub}` : label);
  node.addEventListener('mouseenter', (ev) => tip.show(label, sub, ev.clientX, ev.clientY));
  node.addEventListener('mousemove', (ev) => tip.move(ev.clientX, ev.clientY));
  node.addEventListener('mouseleave', () => tip.hide());
  node.addEventListener('focus', () => { const r = node.getBoundingClientRect(); tip.show(label, sub, r.left + r.width / 2, r.top); });
  node.addEventListener('blur', () => tip.hide());
  if (onOpen) {
    node.addEventListener('click', onOpen);
    node.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); onOpen(); } });
  }
  return node;
}

/** Markers closer than `px` pixels are drawn as one, with all their labels. */
function cluster(items, x, px) {
  const out = [];
  for (const item of items) {
    const xx = x(item.ms);
    const prev = out[out.length - 1];
    if (prev && xx - prev.x1 < px) { prev.items.push(item); prev.x1 = xx; } else out.push({ x0: xx, x1: xx, items: [item] });
  }
  return out.map((c) => ({ ...c, x: (c.x0 + c.x1) / 2 }));
}

function drawContract(svg, m, st, l, x, open, tip) {
  const items = [
    ...m.versions.filter((v) => v.ms <= st.ms).map((v) => ({ ms: v.ms, kind: 'version', label: `v${v.number}`, short: `v${v.number}`,
      sub: v.added.length ? `${plural(v.added.length, 'object')} added` : 'the version at the start of the range' })),
    ...m.changes.filter((c) => c.kind === 'proposal' && c.ms <= st.ms).map((c) => ({ ms: c.ms, kind: 'proposal', label: 'change proposed', short: 'proposal',
      sub: `${SIDE_NAME[c.by] || c.by}: ${c.summary}` })),
  ].sort((a, b) => a.ms - b.ms);
  const clusters = cluster(items, x, 18);
  clusters.forEach((c, i) => {
    const versions = c.items.filter((it) => it.kind === 'version');
    const kind = versions.length ? 'contract' : 'proposal';
    const g = s('g', { class: `tl-marker ${kind}` });
    const cy = l.mid;
    if (kind === 'contract') {
      g.append(s('path', { class: 'shape', d: `M${c.x} ${cy - 9} L${c.x + 9} ${cy} L${c.x} ${cy + 9} L${c.x - 9} ${cy} Z` }));
    } else {
      g.append(s('circle', { class: 'shape', cx: c.x, cy, r: 7 }));
    }
    svg.append(g);
    const next = clusters[i + 1];
    const room = (next ? next.x - 14 : Infinity) - (c.x + 14);
    placeLabel(g, [c.items.map((it) => it.label).join(' · '), c.items.map((it) => it.short).join(' · ')], c.x + 14, cy + 5, room);
    interactive(g, c.items.map((it) => `${it.label} (${elapsed(it.ms)})`).join(', '), c.items.map((it) => it.sub).join(' — '), () => open({ kind: 'contract' }), tip);
  });
}

/** Put the first label that fits in `room` pixels; none when none fits. */
function placeLabel(g, labels, x0, y, room, anchor) {
  for (const label of labels) {
    const text = s('text', { x: x0, y, 'text-anchor': anchor || 'start' }, label);
    g.append(text);
    if (text.getComputedTextLength() <= room) return text;
    text.remove();
  }
  return null;
}

function drawSide(svg, m, st, side, l, x, open, tip) {
  const tickTop = l.y + 3;
  const barTop = l.y + 15;
  const barH = l.h - 19;
  for (const turn of m.turns.filter((t) => t.side === side && t.start <= st.ms)) {
    const end = turn.end === null || turn.end > st.ms ? st.ms : turn.end;
    const running = turn.end === null || turn.end > st.ms;
    const x0 = x(turn.start);
    const w = Math.max(4, x(end) - x0);
    const status = turn.failed && !running ? ' failed' : '';
    const g = s('g', { class: `tl-turn ${side}${status}${running ? ' running' : ''}` });
    g.append(s('rect', { class: 'body', x: x0, y: barTop, width: w, height: barH, rx: 7 }));
    for (const call of turn.calls) {
      if (call.ms > st.ms) break;
      const cx = x(call.ms);
      const err = call.outcome !== 'ok';
      g.append(s('line', { class: `tl-call${err ? ' error' : ''}`, x1: cx, x2: cx, y1: barTop + barH - (err ? barH - 4 : 9), y2: barTop + barH - 3 }));
    }
    svg.append(g);
    // the label, inside the bar when it fits
    const label = running ? `At work · ${plural(turn.calls.filter((c) => c.ms <= st.ms).length, 'model call')}` : turn.label;
    const text = s('text', { x: x0 + 10, y: barTop + barH / 2 + 3 }, label);
    g.append(text);
    const fits = text.getComputedTextLength() + 20 <= w;
    if (!fits) {
      const short = label.split(' · ')[0];
      text.textContent = short;
      if (text.getComputedTextLength() + 20 > w) text.remove();
    }
    const calls = turn.calls.filter((c) => c.ms <= st.ms && c.outcome === 'ok');
    const tin = calls.reduce((a, c) => a + (c.data.prompt_tokens || 0), 0);
    const cost = calls.reduce((a, c) => a + (c.data.cost_usd || 0), 0);
    const state = running ? 'running' : turn.failed ? `failed (${turn.result})` : 'completed';
    interactive(g, `${SIDE_NAME[side]} · turn ${turn.n} · ${state} · ${duration(end - turn.start)}`,
      `${turn.label}. ${plural(calls.length, 'model call')}, ${tokens(tin)} tokens in, ${usd(cost)}.`,
      () => open({ kind: 'turn', key: turn.key }), tip);
  }

  // refusals: red ticks above the bars, with a count per cluster
  const refusals = m.refusals.filter((r) => r.actor === side && r.ms <= st.ms);
  for (const c of cluster(refusals, x, 14)) {
    const g = s('g', { class: 'tl-marker refusal' });
    for (const r of c.items) {
      const rx = x(r.ms);
      g.append(s('line', { class: 'tl-refusal', x1: rx, x2: rx, y1: tickTop, y2: tickTop + 9 }));
    }
    g.append(s('rect', { x: c.x0 - 6, y: tickTop - 3, width: c.x1 - c.x0 + 12, height: 15, fill: 'transparent' }));
    if (c.items.length > 1) {
      // The count goes after the ticks, or before them when the playhead is right there.
      const after = x(st.ms) - c.x1 > 26;
      g.append(s('text', { class: 'tl-count', x: after ? c.x1 + 6 : c.x0 - 6, y: tickTop + 9, 'text-anchor': after ? 'start' : 'end' }, String(c.items.length)));
    }
    const reasons = [...new Set(c.items.map((r) => r.detail || r.reason))].join('; ');
    svg.append(interactive(g, `${plural(c.items.length, 'call')} of the ${SIDE_NAME[side]} refused by the hub (${elapsed(c.items[0].ms)})`, reasons, () => open({ kind: 'refusals' }), tip));
  }

  // the Owner's judgments: inspections and verdicts, as small diamonds above the bar
  if (side === 'owner') {
    const judgments = [];
    for (const d of m.deliveries) {
      for (const r of d.inspections) if (r.ms <= st.ms) judgments.push({ ms: r.ms, status: r.verdict === 'conforms' ? 'good' : 'critical', label: `inspection of ${r.object}: ${r.verdict.replace(/_/g, ' ')}`, id: d.id });
      for (const r of d.reviews) if (r.ms <= st.ms) judgments.push({ ms: r.ms, status: VERDICT_STATUS[r.verdict] || 'neutral', label: `verdict on ${d.id}: ${(VERDICT_WORDS[r.verdict] || r.verdict).toLowerCase()}`, id: d.id });
    }
    judgments.sort((a, b) => a.ms - b.ms);
    for (const j of judgments) {
      const jx = x(j.ms);
      const cy = tickTop + 5;
      const g = s('g', { class: `tl-marker ${j.status}` },
        s('path', { class: 'shape', d: `M${jx} ${cy - 7} L${jx + 7} ${cy} L${jx} ${cy + 7} L${jx - 7} ${cy} Z` }));
      svg.append(interactive(g, `Owner: ${j.label} (${elapsed(j.ms)})`, '', () => open({ kind: 'delivery', id: j.id }), tip));
    }
  }
}

function drawGate(svg, m, st, l, x, open, tip, maxX) {
  const items = [];
  for (const d of m.deliveries) {
    for (const dec of d.decisions) {
      if (dec.ms > st.ms) continue;
      items.push({ ms: dec.ms, status: DECISION_STATUS[dec.decision] || 'neutral', label: dec.decision, id: d.id,
        sub: `${d.id}${dec.after === 'inspection' ? ', after an inspection' : `, ${Object.keys(dec.outcomes).length} tests replayed`}` });
    }
    if (d.declared && d.declared.ms <= st.ms && !d.decisions.some((dec) => dec.ms <= st.ms)) {
      items.push({ ms: d.declared.ms, status: 'neutral', label: `${d.id} declared`, id: d.id, sub: 'the hub replays the Owner\'s tests' });
    }
  }
  items.sort((a, b) => a.ms - b.ms);
  const clusters = cluster(items, x, 26);
  let leftLimit = LEFT;
  clusters.forEach((c, i) => {
    const final = c.items[c.items.length - 1];
    const g = s('g', { class: `tl-marker ${final.status}` });
    if (c.items.length > 1) g.append(s('circle', { class: 'shape', cx: c.x0, cy: l.mid, r: 6, opacity: 0.85 }));
    g.append(s('circle', { class: 'shape', cx: c.x1, cy: l.mid, r: 8 }));
    svg.append(g);
    const label = c.items.map((it) => it.label).join(' → ');
    const labels = c.items.length > 1 ? [label, final.label] : [label];
    const next = clusters[i + 1];
    const roomRight = Math.min(next ? next.x0 - 14 : maxX, maxX) - (c.x1 + 14);
    const roomLeft = c.x0 - 14 - leftLimit;
    let placed = null;
    for (const text of labels) {
      placed = placeLabel(g, [text], c.x1 + 14, l.mid + 5, roomRight) || placeLabel(g, [text], c.x0 - 14, l.mid + 5, roomLeft, 'end');
      if (placed) break;
    }
    leftLimit = placed && placed.getAttribute('text-anchor') !== 'end' ? c.x1 + 14 + placed.getComputedTextLength() + 10 : c.x1 + 12;
    interactive(g, `Gate: ${label}`, c.items.map((it) => `${it.label} at ${elapsed(it.ms)} (${it.sub})`).join('; '), () => open({ kind: 'delivery', id: final.id }), tip);
  });
}
