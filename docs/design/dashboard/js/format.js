// Formatting of times, counts and money. The dashboard shows elapsed time since the start of the
// run (never the viewer's local clock: a screenshot or a video must not tell where it was made),
// and UTC clock times in the details.

const pad = (n) => String(n).padStart(2, '0');

/** "+48:35", "+1:02:58": elapsed time since the start of the run. */
export function elapsed(ms) {
  const total = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  return h ? `+${h}:${pad(m)}:${pad(sec)}` : `+${m}:${pad(sec)}`;
}

/** "1 h 09 min", "18 min 40 s", "51 s". */
export function duration(ms) {
  const total = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  if (h) return `${h} h ${pad(m)} min`;
  if (m) return m >= 10 ? `${m} min` : `${m} min ${pad(sec)} s`;
  return `${sec} s`;
}

/** "12.7 M", "129 k", "870". */
export function tokens(n) {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} M`;
  if (n >= 1e4) return `${Math.round(n / 1e3)} k`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)} k`;
  return String(n);
}

export function usd(x) {
  return `$${(x || 0).toFixed(2)}`;
}

export function int(n) {
  return Number(n || 0).toLocaleString('en-US');
}

export function plural(n, one, many) {
  return `${int(n)} ${n === 1 ? one : (many || one + 's')}`;
}

/** "08:57:38 UTC" from an ISO 8601 UTC time. */
export function utc(iso) {
  return typeof iso === 'string' && iso.length >= 19 ? `${iso.slice(11, 19)} UTC` : '';
}

/** A test identifier ("file.py::test_name") as words. */
export function testWords(id) {
  const name = String(id).split('::').pop() || String(id);
  return name.replace(/^test_/, '').replace(/_/g, ' ');
}

export function capitalize(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

export const SIDE_NAME = { owner: 'Owner', contractor: 'Contractor', operator: 'Operator', hub: 'Hub', 'main-agent': 'Main agent' };

/** A party inside a sentence: the two sides keep their capital, as in the documentation. */
export function roleName(actor) {
  if (actor === 'owner' || actor === 'contractor') return SIDE_NAME[actor];
  return (SIDE_NAME[actor] || String(actor || '')).toLowerCase();
}
export const VERDICT_WORDS = { accept: 'Accept', accept_with_reservations: 'Accept with reservations', reject: 'Reject' };
