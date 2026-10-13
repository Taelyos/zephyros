// Entry point of the mockup: load a replay, build the read model, render the overview, and keep
// it in step with the playhead.
//
// URL parameters (all optional), so that a screenshot or a recording shows a chosen moment:
//   at=end|start|<seconds>|e<event id>   where the playhead starts (default: end)
//   play=1, speed=1|10|30|60|120|300     play from there
//   mode=live                            the live view: no transport, the clock runs at speed 1
//   open=delivery:d2|turn:owner:3|contract|refusals|moments|req:EXG-06
//   data=<file>.replay.json              another replay in data/
//   hostile=1                            plant the strings of ATK-67 in the sample

import { h, replace } from './dom.js';
import { loadReplay, stateAt } from './model.js';
import { renderHeader, renderGate, renderSide, renderRun, renderRequirements, renderMoments } from './overview.js';
import { createTimeline } from './timeline.js';
import { createDrawers } from './drawers.js';
import { createPlayer } from './player.js';
import { plantHostileStrings } from './hostile.js';

const params = new URLSearchParams(location.search);
const mode = params.get('mode') === 'live' ? 'live' : 'replay';
const dataName = /^[a-z0-9][a-z0-9_.-]{0,80}\.replay\.json$/.test(params.get('data') || '') ? params.get('data') : 'run3.replay.json';

boot();

async function boot() {
  let doc;
  try {
    const response = await fetch(`data/${dataName}`, { cache: 'no-store', credentials: 'same-origin', redirect: 'error' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    doc = await response.json();
  } catch (error) {
    fail(`Could not load data/${dataName} (${error.message}). Serve this folder over HTTP: see README.md.`);
    return;
  }
  if (params.get('hostile') === '1') plantHostileStrings(doc);
  let model;
  try {
    model = loadReplay(doc);
  } catch (error) {
    fail(error.message);
    return;
  }
  start(model);
}

function fail(message) {
  replace(document.getElementById('boot'), h('div', null, h('p', null, 'This page cannot open the replay.'), h('p', null, message)));
}

function start(m) {
  document.getElementById('boot').hidden = true;
  const app = document.getElementById('app');
  app.hidden = false;
  app.classList.toggle('live', mode === 'live');
  renderHeader(m, mode);

  let state = stateAt(m, m.durationMs);
  const tip = createTip();
  const drawers = createDrawers(m, { getState: () => state });
  const open = (spec) => drawers.open(spec);
  const timeline = createTimeline(document.getElementById('timeline'), m, { open, tip });
  const announcer = document.getElementById('announcer');

  let lastKey = '';
  let lastDecision = '';
  function onTime(ms) {
    // Re-render when an event is crossed, and every few seconds of the run for running clocks.
    const crossed = m.events.filter((e) => e.ms <= ms).length;
    const key = `${crossed}|${Math.floor(ms / 5000)}`;
    if (key === lastKey) return;
    lastKey = key;
    state = stateAt(m, ms);
    renderGate(m, state, open);
    renderSide(m, state, 'owner', open);
    renderSide(m, state, 'contractor', open);
    renderRun(m, state, mode);
    renderRequirements(m, state, open);
    renderMoments(m, state, open);
    timeline.render(state, mode);
    drawers.refresh();
    const decision = state.decision ? `${state.delivery.id}:${state.decisionIndex}` : '';
    if (decision && decision !== lastDecision) announcer.textContent = `Gate: ${state.decision.decision} for ${state.delivery.id}`;
    lastDecision = decision;
  }

  const player = createPlayer(m, { onTime, mode, speed: Number(params.get('speed')) || undefined });
  // Sizes are measured (lanes, the rows that fit): draw again when the board changes size.
  new ResizeObserver(() => { lastKey = ''; onTime(player.playhead); }).observe(document.getElementById('board'));
  player.seek(startAt(m, params.get('at')));
  const spec = openSpec(m, params.get('open'));
  if (spec) open(spec);
  if (params.get('play') === '1' || mode === 'live') player.play();

  // The recorder of PR #6 (tools/replay/record.mjs) drives a page through this interface.
  const caption = document.getElementById('caption');
  window.replay = {
    play: () => player.play(),
    pause: () => player.pause(),
    seek: (eventId) => { const e = m.byId.get(eventId); if (e) player.seek(e.ms); },
    focus: (eventId) => { const e = m.byId.get(eventId); if (e) { player.pause(); player.seek(e.ms); } },
    setSpeed: (v) => player.setSpeed(v),
    open: (text) => { const s = openSpec(m, text); if (s) open(s); },
    close: () => drawers.close(),
    caption: (text) => { caption.hidden = !text; replace(caption, text || ''); },
    state: () => {
      const crossed = m.events.filter((e) => e.ms <= player.playhead);
      return { eventId: crossed.length ? crossed[crossed.length - 1].id : null, playing: player.playing, done: player.done };
    },
  };
}

function startAt(m, value) {
  if (!value || value === 'end') return m.durationMs;
  if (value === 'start') return 0;
  const byEvent = /^e(\d+)$/.exec(value);
  if (byEvent) { const e = m.byId.get(Number(byEvent[1])); return e ? e.ms : m.durationMs; }
  const seconds = Number(value);
  return Number.isFinite(seconds) ? seconds * 1000 : m.durationMs;
}

function openSpec(m, value) {
  if (!value) return null;
  const [kind, a, b] = value.split(':');
  if (kind === 'delivery' && a) return { kind, id: a };
  if (kind === 'req' && a) return { kind: 'requirement', id: a };
  if (kind === 'turn' && a) {
    const turn = m.turns.find((t) => t.side === a && t.n === Number(b || 1));
    return turn ? { kind, key: turn.key } : null;
  }
  if (['contract', 'refusals', 'moments'].includes(kind)) return { kind };
  return null;
}

function createTip() {
  const node = document.getElementById('tooltip');
  const tip = {
    show(title, sub, x, y) {
      replace(node, h('div', { class: 'tt-title' }, title), sub ? h('div', { class: 'tt-sub' }, sub) : null);
      node.hidden = false;
      tip.move(x, y);
    },
    move(x, y) {
      const r = node.getBoundingClientRect();
      let left = x + 14;
      let top = y + 18;
      if (left + r.width > window.innerWidth - 8) left = x - r.width - 14;
      if (top + r.height > window.innerHeight - 8) top = y - r.height - 14;
      node.style.left = `${Math.max(8, left)}px`;
      node.style.top = `${Math.max(8, top)}px`;
    },
    hide() { node.hidden = true; },
  };
  return tip;
}
