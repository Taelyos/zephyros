// `?hostile=1`: plant the strings of ATK-67 in the sample, where agents and clients can write
// them (a requirement, a branch name, an agent's message, an inspection, a refusal's detail, the
// address of a pull request). The page must show them as text, run nothing and load nothing.

const EVIL = {
  script: '<script>alert("xss")</script>',
  img: '<img src=x onerror=alert(1)>',
  markdown: '![beacon](https://attacker.example/pixel.png) [open me](javascript:alert(1))',
  css: '<style>body{display:none}</style>',
  link: '<a href="javascript:alert(1)">click</a>',
  bidi: 'report‮fdp.exe',
};

export function plantHostileStrings(doc) {
  const objects = [doc.contract && doc.contract.objects, ...doc.events.filter((e) => e.type === 'contract.version').map((e) => e.data.objects)];
  for (const list of objects) {
    for (const o of list || []) {
      if (o.id === 'EXG-01') o.text = `${EVIL.script} ${o.text}`;
      if (o.id === 'US-E1-01') o.title = `${EVIL.img} ${o.title}`;
    }
  }
  let message = false;
  let refusal = false;
  for (const e of doc.events) {
    const d = e.data || {};
    if (d.branch) d.branch = `${EVIL.img}${d.branch}`;
    if (e.type === 'pull_request.open') d.url = 'javascript:alert(document.cookie)';
    if (e.type === 'agent.message' && e.actor === 'contractor' && !message) { d.text = `${EVIL.markdown} ${EVIL.css} ${d.text}`; message = true; }
    if (e.outcome === 'refused' && !refusal) { d.detail = `no such path ${EVIL.bidi}`; refusal = true; }
    if (e.type === 'inspection.record') d.observation = `${EVIL.link} ${d.observation}`;
    if (e.type === 'delivery.review' && e.outcome === 'ok') d.reasons = `${EVIL.css}${EVIL.script} ${d.reasons}`;
  }
  doc.run.title = `${doc.run.title} ${EVIL.link}`;
}
