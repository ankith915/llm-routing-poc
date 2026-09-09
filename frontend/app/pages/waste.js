import { get } from '../api.js';
import { card, chip, empty, esc, int, money, stat } from '../ui.js';
import { ranked } from '../charts.js';

const SEV = { high: 'risk', medium: 'warn', low: '' };

export async function render(root, state) {
  const w = await get('/api/analytics/waste');
  if (!w.findings?.length) {
    root.innerHTML = head(w) + card('', empty(
      w.requests ? 'Nothing wasteful found in this traffic' : 'No traffic to analyse yet',
      w.requests
        ? `Across ${int(w.requests)} recorded requests, none of the waste patterns fired. That is a
           real result on this workload, not an empty page.`
        : 'Run the demo workload and this page fills with findings derived from those requests.',
      w.requests ? '' : '<button class="btn primary" data-go="overview">Go to overview</button>'), { pad: false });
    return;
  }
  const bars = w.findings.filter((f) => f.avoidable_cost_usd > 0)
    .map((f) => ({ label: f.title, value: f.avoidable_cost_usd,
      color: f.severity === 'high' ? 'var(--risk)' : f.severity === 'medium' ? 'var(--warn)' : 'var(--ink-4)',
      sub: `${f.confident ? '' : 'low confidence · '}${int(f.sample)} observation(s)` }));

  root.innerHTML = head(w) + `
    <div class="grid g3">
      ${stat({ label: 'Spend analysed', value: money(w.total_cost_usd), sub: `${int(w.requests)} requests` })}
      ${stat({ label: 'Avoidable', value: money(w.total_avoidable_usd), tone: 'warn',
        sub: 'projected from the requests actually recorded' })}
      ${stat({ label: 'Findings', value: int(w.findings.length),
        sub: `${w.findings.filter((f) => f.severity === 'high').length} high severity` })}
    </div>

    ${bars.length ? `<div class="section">${card('Avoidable cost by cause',
      ranked(bars, { valueFmt: money }))}</div>` : ''}

    <div class="section">
      <div class="head"><div><h2>Findings</h2>
        <p>Each one states the evidence it rests on. A finding marked low confidence has fewer than
          three observations behind it and should be treated as a hypothesis, not a number.</p></div></div>
      <div class="grid" style="gap:12px">
        ${w.findings.map(finding).join('')}
      </div>
    </div>
    <div class="note" style="margin-top:16px">${esc(w.note)}</div>`;
}

function head(w) {
  return `<div class="page-head"><div>
    <div class="kicker">Waste analysis</div>
    <h1>What is costing us money that does not need to?</h1>
    <p class="lede">Derived from stored request records. Nothing here is a generic best-practice
      list: if a pattern did not occur in your traffic, it is not on this page.</p>
  </div><div class="btn-row">
    <button class="btn primary" data-go="recommendations">See what to do about it</button>
  </div></div>`;
}

function finding(f) {
  return `<div class="card"><div class="pad">
    <div style="display:flex;justify-content:space-between;gap:16px;align-items:flex-start">
      <div style="min-width:0">
        <div class="btn-row" style="margin-bottom:6px">
          ${chip(f.severity, SEV[f.severity])}
          ${chip(f.basis, f.basis === 'measured' ? 'save' : 'warn')}
          ${f.confident ? '' : chip('low confidence', 'warn')}
        </div>
        <h3 style="margin:0 0 4px;font-size:14px;font-weight:600">${esc(f.title)}</h3>
        <div class="note">${esc(f.evidence)}</div>
      </div>
      <div style="text-align:right;white-space:nowrap">
        <div class="l mono" style="font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;
          color:var(--ink-4)">Avoidable</div>
        <div class="num" style="font-size:19px;font-weight:600;color:${f.avoidable_cost_usd > 0
          ? 'var(--warn)' : 'var(--ink-3)'}">${money(f.avoidable_cost_usd)}</div>
        <div class="note">of ${money(f.observed_cost_usd)} observed</div>
      </div>
    </div>
    <div class="callout" style="margin-top:12px">${esc(f.recommended_action)}</div>
    ${detailBlock(f.detail)}
  </div></div>`;
}

function detailBlock(d) {
  if (!d || !Object.keys(d).length) return '';
  const rows = Object.entries(d).filter(([, v]) => v !== null && v !== undefined
    && !(Array.isArray(v) && !v.length) && !(typeof v === 'object' && !Array.isArray(v) && !Object.keys(v).length));
  if (!rows.length) return '';
  return `<details class="det" style="margin-top:10px"><summary>evidence</summary>
    <dl class="kv" style="margin-top:8px">${rows.map(([k, v]) => `
      <dt>${esc(k.replace(/_/g, ' '))}</dt><dd>${fmt(v)}</dd>`).join('')}</dl></details>`;
}

function fmt(v) {
  if (Array.isArray(v)) {
    return v.map((x) => typeof x === 'object'
      ? Object.entries(x).map(([k, y]) => `${esc(k)} ${esc(String(y))}`).join(', ')
      : esc(String(x))).map((s) => `<div>${s}</div>`).join('');
  }
  if (typeof v === 'object') {
    return Object.entries(v).map(([k, y]) =>
      `<span class="chip" style="margin-right:4px">${esc(k)} ${esc(String(y))}</span>`).join('');
  }
  if (typeof v === 'number' && v < 1 && v > 0) return `<span class="num">${v}</span>`;
  return esc(String(v));
}
