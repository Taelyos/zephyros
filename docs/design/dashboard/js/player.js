// The replay transport: play, pause, speed, key moments, and a scrubber. In live mode the same
// clock runs at the speed of the run and the transport is hidden: what the live dashboard shows
// is the state "now", updated as records arrive.

import { h, icon, replace } from './dom.js';
import { elapsed, utc } from './format.js';

const SPEEDS = [1, 10, 30, 60, 120, 300];

export function createPlayer(m, { onTime, mode, speed: initialSpeed }) {
  const end = m.durationMs;
  let playhead = end;
  let speed = SPEEDS.includes(initialSpeed) ? initialSpeed : mode === 'live' ? 1 : 60;
  let playing = false;
  let last = 0;
  const startIso = m.run.started_at;

  const transport = document.getElementById('transport');
  const range = h('input', { type: 'range', id: 'scrub-range', min: 0, max: end, step: 1000, 'aria-label': 'Position in the run' });
  const ticks = h('div', { class: 'scrub-ticks', 'aria-hidden': 'true' });
  for (const x of m.moments) {
    if (x.status === 'neutral') continue;
    const tick = h('span', { class: `scrub-tick ${x.status}`, title: `${x.who} ${x.text}` });
    tick.style.left = `${(x.ms / end) * 100}%`;
    ticks.append(tick);
  }
  const playBtn = h('button', { class: 'tbtn main', type: 'button', 'aria-label': 'Play' });
  const prevBtn = h('button', { class: 'tbtn', type: 'button', 'aria-label': 'Previous key moment', title: 'Previous key moment (←)' }, icon('prev'));
  const nextBtn = h('button', { class: 'tbtn', type: 'button', 'aria-label': 'Next key moment', title: 'Next key moment (→)' }, icon('next'));
  const speeds = h('div', { class: 'speeds', role: 'group', 'aria-label': 'Speed' }, SPEEDS.map((v) => h('button', {
    type: 'button', 'aria-pressed': String(v === speed), on: { click: () => setSpeed(v) },
  }, `×${v}`)));
  const clock = h('div', { class: 'clock', 'aria-live': 'off' });
  replace(transport, playBtn, prevBtn, nextBtn, speeds, h('div', { class: 'scrub' }, ticks, range), clock);
  if (mode === 'live') transport.hidden = true;

  playBtn.addEventListener('click', () => (playing ? pause() : play()));
  prevBtn.addEventListener('click', () => jump(-1));
  nextBtn.addEventListener('click', () => jump(1));
  range.addEventListener('input', () => { pause(); seek(Number(range.value)); });
  document.addEventListener('keydown', (ev) => {
    if (ev.target instanceof HTMLInputElement || mode === 'live') return;
    if (document.getElementById('drawer').hidden === false) return;
    if (ev.key === ' ' && !(ev.target instanceof HTMLButtonElement)) { ev.preventDefault(); playing ? pause() : play(); }
    if (ev.key === 'ArrowRight') { ev.preventDefault(); jump(1); }
    if (ev.key === 'ArrowLeft') { ev.preventDefault(); jump(-1); }
  });

  function jump(direction) {
    pause();
    const keys = m.moments.filter((x) => x.status !== 'neutral' || x.icon === 'box' || x.icon === 'doc');
    const target = direction > 0 ? keys.find((x) => x.ms > playhead + 500) : [...keys].reverse().find((x) => x.ms < playhead - 500);
    seek(target ? target.ms : direction > 0 ? end : 0);
  }

  function update() {
    range.value = String(Math.round(playhead));
    range.style.setProperty('--fill', `${(playhead / end) * 100}%`);
    const at = startIso ? new Date(Date.parse(startIso) + playhead).toISOString() : '';
    replace(clock, h('b', null, elapsed(playhead)), ` / ${elapsed(end).slice(1)}`, at ? h('div', null, utc(at)) : null);
    replace(playBtn, icon(playing ? 'pause' : 'play'));
    playBtn.setAttribute('aria-label', playing ? 'Pause' : 'Play');
    onTime(playhead);
  }

  function frame(ts) {
    if (!playing) return;
    playhead = Math.min(end, playhead + (ts - last) * speed);
    last = ts;
    if (playhead >= end) playing = false;
    update();
    if (playing) requestAnimationFrame(frame);
  }

  function play() {
    if (playing) return;
    if (playhead >= end) playhead = 0;
    playing = true;
    last = performance.now();
    update();
    requestAnimationFrame(frame);
  }

  function pause() {
    if (!playing) return;
    playing = false;
    update();
  }

  function seek(ms) {
    playhead = Math.max(0, Math.min(end, ms));
    update();
  }

  function setSpeed(v) {
    speed = v;
    for (const b of speeds.children) b.setAttribute('aria-pressed', String(b.textContent === `×${v}`));
  }

  return {
    play, pause, seek, setSpeed,
    get playhead() { return playhead; },
    get playing() { return playing; },
    get done() { return playhead >= end; },
  };
}
