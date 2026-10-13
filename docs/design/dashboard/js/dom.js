// Safe DOM construction. Every string from the data (log, contract, transcripts) is untrusted:
// it only ever becomes a text node. No innerHTML, no insertAdjacentHTML, no eval, no inline
// handlers. Attributes come from a closed list; a link is an https URL that passes `safeUrl`.

const SVG_NS = 'http://www.w3.org/2000/svg';

const ATTRS = new Set([
  'class', 'id', 'role', 'tabindex', 'type', 'title', 'for', 'name', 'value', 'disabled', 'hidden',
  'lang', 'dir', 'href', 'target', 'rel', 'min', 'max', 'step', 'colspan', 'scope',
  // SVG geometry and presentation
  'width', 'height', 'viewBox', 'd', 'x', 'y', 'x1', 'y1', 'x2', 'y2', 'cx', 'cy', 'r', 'rx', 'ry',
  'points', 'transform', 'fill', 'stroke', 'stroke-width', 'stroke-linecap', 'stroke-linejoin',
  'opacity', 'text-anchor', 'dominant-baseline', 'focusable', 'preserveAspectRatio',
  'patternUnits', 'patternTransform', 'xmlns',
]);

function setAttributes(node, attrs) {
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'on') {
      for (const [event, handler] of Object.entries(value)) node.addEventListener(event, handler);
      continue;
    }
    if (key === 'dataset') {
      for (const [k, v] of Object.entries(value)) node.dataset[k] = String(v);
      continue;
    }
    const allowed = ATTRS.has(key) || key.startsWith('aria-');
    if (!allowed) throw new Error(`attribute not allowed: ${key}`);
    if (key === 'href') {
      const url = safeUrl(value);
      if (!url) continue; // an unsafe link is shown as text by the caller, never followed
      node.setAttribute('href', url);
      node.setAttribute('rel', 'noopener noreferrer');
      continue;
    }
    node.setAttribute(key, value === true ? '' : String(value));
  }
}

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

/** An HTML element. Children that are not nodes become text nodes. */
export function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  setAttributes(node, attrs);
  append(node, children);
  return node;
}

/** An SVG element. */
export function s(tag, attrs, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  setAttributes(node, attrs);
  append(node, children);
  return node;
}

export function clear(node) {
  while (node.firstChild) node.firstChild.remove();
  return node;
}

export function replace(node, ...children) {
  clear(node);
  append(node, children);
  return node;
}

/** An https URL on the forge, or null. The data may hold any string in a `url` field. */
export function safeUrl(value) {
  if (typeof value !== 'string') return null;
  let url;
  try { url = new URL(value); } catch { return null; }
  if (url.protocol !== 'https:' || url.username || url.password) return null;
  if (url.hostname !== 'github.com') return null;
  return url.href;
}

// ---------------------------------------------------------------------------------- icons
// Simple 24 x 24 strokes, drawn for this mockup.
const ICONS = {
  check: [['path', { d: 'M5 12.5l4.5 4.5L19 7.5' }]],
  checkCircle: [['circle', { cx: 12, cy: 12, r: 9.5 }], ['path', { d: 'M7.8 12.3l2.8 2.8 5.6-5.8' }]],
  cross: [['path', { d: 'M6.5 6.5l11 11M17.5 6.5l-11 11' }]],
  crossCircle: [['circle', { cx: 12, cy: 12, r: 9.5 }], ['path', { d: 'M9 9l6 6M15 9l-6 6' }]],
  half: [['circle', { cx: 12, cy: 12, r: 9.5 }], ['path', { d: 'M12 2.5a9.5 9.5 0 0 1 0 19z', fill: 'currentColor', stroke: 'none' }]],
  ring: [['circle', { cx: 12, cy: 12, r: 9 }]],
  ban: [['circle', { cx: 12, cy: 12, r: 9.5 }], ['path', { d: 'M5.3 5.3l13.4 13.4' }]],
  alert: [['path', { d: 'M12 3.2L2.6 19.5h18.8z' }], ['path', { d: 'M12 9.5v4.6M12 16.9v.2' }]],
  clock: [['circle', { cx: 12, cy: 12, r: 9.5 }], ['path', { d: 'M12 7v5.2l3.4 2' }]],
  doc: [['path', { d: 'M6 2.8h8l4.2 4.2v14.2H6z' }], ['path', { d: 'M13.8 2.8v4.5h4.4M9 12h6.5M9 15.5h6.5M9 8.6h2.5' }]],
  box: [['path', { d: 'M12 2.8l8.4 4.6v9.2L12 21.2l-8.4-4.6V7.4z' }], ['path', { d: 'M3.8 7.5L12 12l8.2-4.5M12 12v9' }]],
  eye: [['path', { d: 'M1.8 12S5.6 5 12 5s10.2 7 10.2 7-3.8 7-10.2 7S1.8 12 1.8 12z' }], ['circle', { cx: 12, cy: 12, r: 3 }]],
  gavel: [['path', { d: 'M13.5 3.5l7 7M10.5 6.5l7 7M12 5l-6.5 6.5 4 4L16 9M7.5 13.5L2.8 18.2l3 3 4.7-4.7M13 20.5h8' }]],
  pr: [['circle', { cx: 6, cy: 5.5, r: 2.3 }], ['circle', { cx: 6, cy: 18.5, r: 2.3 }], ['circle', { cx: 18, cy: 18.5, r: 2.3 }], ['path', { d: 'M6 7.8v8.4M18 16.2V9.5a3 3 0 0 0-3-3h-4M13 4l-2.4 2.5L13 9' }]],
  flask: [['path', { d: 'M9.5 2.8h5M10.3 2.8v6L4.6 18.6a1.8 1.8 0 0 0 1.6 2.6h11.6a1.8 1.8 0 0 0 1.6-2.6L13.7 8.8v-6M7.5 14h9' }]],
  play: [['path', { d: 'M7 4.5v15l12.5-7.5z', fill: 'currentColor', stroke: 'none' }]],
  pause: [['path', { d: 'M7 4.5h3.5v15H7zM13.5 4.5H17v15h-3.5z', fill: 'currentColor', stroke: 'none' }]],
  next: [['path', { d: 'M5 5l10 7-10 7z', fill: 'currentColor', stroke: 'none' }], ['path', { d: 'M18.5 5v14' }]],
  prev: [['path', { d: 'M19 5L9 12l10 7z', fill: 'currentColor', stroke: 'none' }], ['path', { d: 'M5.5 5v14' }]],
  chevron: [['path', { d: 'M9 5.5l6.5 6.5L9 18.5' }]],
  arrow: [['path', { d: 'M4 12h15M13.5 6.5L19 12l-5.5 5.5' }]],
  chain: [['path', { d: 'M10 14.2a4.2 4.2 0 0 0 6 .2l3-3a4.2 4.2 0 0 0-6-6l-1.5 1.5' }], ['path', { d: 'M14 9.8a4.2 4.2 0 0 0-6-.2l-3 3a4.2 4.2 0 0 0 6 6l1.5-1.5' }]],
  speech: [['path', { d: 'M4 4.5h16v11.5H10l-5 4v-4H4z' }]],
  spinner: [['path', { d: 'M12 3a9 9 0 1 1-9 9' }]],
  shield: [['path', { d: 'M12 2.8l7.5 3v6c0 4.6-3.2 8-7.5 9.4C7.7 19.8 4.5 16.4 4.5 11.8v-6z' }]],
  play2: [['path', { d: 'M8 5v14l11-7z' }]],
  close: [['path', { d: 'M6 6l12 12M18 6L6 18' }]],
  key: [['circle', { cx: 8, cy: 15, r: 4 }], ['path', { d: 'M11 12l8.5-8.5M16 7l2.5 2.5M14 9l2 2' }]],
};

/** An icon as an inline SVG (decorative: hidden from assistive technology). */
export function icon(name) {
  const parts = ICONS[name] || ICONS.ring;
  return s('svg', {
    viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2,
    'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', focusable: 'false',
  }, parts.map(([tag, attrs]) => s(tag, attrs)));
}

/** A status chip: an icon in the status colour and a word in text colour. */
export function chip(status, iconName, label, extraClass) {
  return h('span', { class: `chip ${status}${extraClass ? ' ' + extraClass : ''}` }, icon(iconName), label);
}
