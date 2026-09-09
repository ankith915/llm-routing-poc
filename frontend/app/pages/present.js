// Presentation mode: a guided run of the real system, one beat at a time.
// Every panel here is the outcome of a scenario that just executed. If a
// scenario cannot make its point on this run, it says so rather than pretending.
import { all, get, post } from '../api.js';
import { basisChip, card, chip, esc, int, money, ms, pct, stat, table, tierChip, toast, truncate } from '../ui.js';
import { waterfall } from '../charts.js';
import { openTrace } from './trace.js';
import { go } from '../main.js';

const results = {};
let step = 0;
let scenarios = [];

const BEATS = [
  { key: 'intro', title: 'The problem' },
  { key: 'baseline', title: 'Today' },
  { key: 'optimized', title: 'Optimized' },
  { key: 'cache', title: 'Repeat traffic' },
  { key: 'escalation', title: 'Quality' },
  { key: 'failover', title: 'Reliability' },
  { key: 'budget', title: 'Budget' },
  { key: 'sla', title: 'SLA' },
  { key: 'waterfall', title: 'Where it came from' },
  { key: 'shadow', title: 'Safe adoption' },
  { key: 'close', title: 'The ask' },
];

export async function render(root, state) {
  if (!scenarios.length) {
    try { scenarios = (await get('/api/demo/scenarios')).scenarios; } catch { scenarios = []; }
  }
  step = Math.max(0, Math.min(BEATS.length - 1, Number(state.params.step ?? step) || 0));
  const beat = BEATS[step];
  root.innerHTML = `<div id="stage">${await stage(beat)}</div>${bar()}`;
  wire(root);
}

function bar() {
  return `<div class="present-bar">
    <button class="btn ghost sm" data-nav="prev" ${step === 0 ? 'disabled' : ''}>◀</button>
    <div class="steps">${BEATS.map((b, i) =>
      `<button class="${i === step ? 'on' : (results[b.key] || i < step ? 'done' : '')}"
        data-step="${i}">${i + 1}. ${esc(b.title)}</button>`).join('')}</div>
    <button class="btn ghost sm" data-nav="next" ${step === BEATS.length - 1 ? 'disabled' : ''}>▶</button>
    <button class="btn sm" data-nav="reset">Reset demo</button>
    <button class="btn sm" data-go="overview">Exit</button>
  </div>`;
}

function wire(root) {
  root.querySelectorAll('[data-step]').forEach((b) => {
    b.onclick = () => go('present', { step: b.dataset.step });
  });
  root.querySelectorAll('[data-nav]').forEach((b) => {
    b.onclick = async () => {
      if (b.dataset.nav === 'reset') {
        await post('/api/demo/reset?full=true');
        Object.keys(results).forEach((k) => delete results[k]);
        toast('Demo reset: request log, caches and budgets cleared.');
        return go('present', { step: 0 });
      }
      go('present', { step: String(step + (b.dataset.nav === 'next' ? 1 : -1)) });
    };
  });
  const run = root.querySelector('#run-beat');
  if (run) run.onclick = () => runBeat(root, run.dataset.key);
  root.querySelectorAll('[data-trace]').forEach((b) => {
    b.onclick = () => openTrace(b.dataset.trace);
  });
  document.onkeydown = (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    if (e.key === 'ArrowRight' && step < BEATS.length - 1) go('present', { step: String(step + 1) });
    if (e.key === 'ArrowLeft' && step > 0) go('present', { step: String(step - 1) });
  };
}

async function runBeat(root, key) {
  const btn = root.querySelector('#run-beat');
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Running live…';
  try {
    results[key] = await post(`/api/demo/scenario/${key}`);
    go('present', { step: String(step) });
  } catch (e) {
    toast(e.message);
    btn.disabled = false;
    btn.textContent = 'Run it';
  }
}

// ------------------------------------------------------------------- beats
async function stage(beat) {
  if (beat.key === 'intro') return intro();
  if (beat.key === 'close') return close();
  if (beat.key === 'waterfall') return waterfallBeat();
  const meta = scenarios.find((s) => s.key === beat.key) || {};
  const r = results[beat.key];
  if (!r) {
    return shell(meta.title || beat.title, meta.question || '', `
      <div class="card"><div class="pad" style="text-align:center;padding:56px 24px">
        <div class="note" style="max-width:60ch;margin:0 auto 22px;font-size:13px;line-height:1.8">
          <b style="color:var(--ink);display:block;font-size:14px;margin-bottom:8px">
            What this should show</b>${esc(meta.criterion || '')}</div>
        <button class="btn primary" id="run-beat" data-key="${esc(beat.key)}">Run it live</button>
        <div class="note" style="margin-top:14px">This executes real requests through the real
          pipeline. Whatever comes back is what you will see.</div>
      </div></div>`);
  }
  return shell(r.title, r.question, scenarioBody(r));
}

function shell(title, question, body) {
  return `<div class="page-head"><div>
    <div class="kicker">Step ${step + 1} of ${BEATS.length}</div>
    <h1 style="font-size:var(--t-hero);max-width:22ch">${esc(title)}</h1>
    ${question ? `<p class="lede" style="font-size:15px;max-width:70ch">${esc(question)}</p>` : ''}
  </div></div>${body}`;
}

function scenarioBody(r) {
  const m = r.metrics || {};
  return `
    <div class="callout ${r.passed ? 'save' : 'warn'}" style="font-size:14px;line-height:1.7;
      padding:16px 20px;margin-bottom:20px">${esc(r.verdict)}</div>
    ${r.note ? `<div class="note" style="margin:-8px 0 20px;font-size:12.5px">${esc(r.note)}</div>` : ''}
    ${metricTiles(m)}
    <div class="section">
      ${card('The requests that just ran', table([
        { label: 'Question', cell: (x) => `<div>${esc(truncate(x.query, 62))}</div>
            <div class="note mono" style="font-size:10px">${esc(x.request_id)}</div>` },
        { label: 'Routed to', cell: (x) => x.cache_kind
            ? chip(`${x.cache_kind} cache`, 'info') : tierChip(x.tier) },
        { label: 'Model', cell: (x) => `<span class="mono" style="font-size:11px">${esc((x.model || '').split('/').pop() || '—')}</span>` },
        { label: 'Cost', align: 'r', cell: (x) => `<b class="num">${money(x.cost_usd)}</b>
            <div class="note num" style="font-size:10px">of ${money(x.baseline_cost_usd)}</div>` },
        { label: 'Saved', align: 'r', cell: (x) => `<span class="num" style="color:var(--save)">${pct(x.savings_pct, 0)}</span>` },
        { label: 'Quality', align: 'r', cell: (x) => x.quality_score != null
            ? `<span class="num" style="color:${x.gate_passed === false ? 'var(--risk)' : 'var(--save)'}">${x.quality_score}</span>` : '—' },
        { label: '', cell: (x) => [
            x.escalated_from ? chip('escalated', 'warn') : '',
            x.fallback_used ? chip('fallback', 'warn') : '',
            x.execution_mode && x.execution_mode !== 'interactive' ? chip(x.execution_mode, 'info') : '',
            `<button class="btn ghost sm" data-trace="${esc(x.request_id)}"
               style="padding:2px 8px;font-size:10px">trace</button>`,
          ].join(' ') },
      ], r.requests || [], { minWidth: 900 }), { pad: false })}
    </div>
    <div class="btn-row" style="margin-top:16px">
      <button class="btn sm" id="run-beat" data-key="${esc(r.key)}">Run it again</button>
      <span class="note">Ran in ${ms(r.elapsed_ms)}. These are live results, not a recording.</span>
    </div>`;
}

function metricTiles(m) {
  const tiles = Object.entries(m).filter(([, v]) =>
    v !== null && v !== undefined && typeof v !== 'object').slice(0, 4);
  if (!tiles.length) return '';
  return `<div class="grid g4">${tiles.map(([k, v]) => stat({
    label: k.replace(/_/g, ' '),
    value: typeof v === 'boolean' ? (v ? 'yes' : 'no')
      : (k.includes('usd') || k.includes('cost') ? money(v)
        : k.includes('pct') || k.includes('rate') ? pct(v) : String(v)),
    tone: typeof v === 'boolean' ? (v ? 'good' : 'warn') : '',
  })).join('')}</div>`;
}

function intro() {
  return `<div class="page-head"><div>
    <div class="kicker">Step 1 of ${BEATS.length}</div>
    <h1 style="font-size:var(--t-hero);max-width:20ch">We are not choosing cheaper models.</h1>
    <p class="lede" style="font-size:16px;max-width:66ch">We are deciding, per request, the cheapest
      safe path to the outcome the business needs — and showing the receipt for every one.</p>
  </div></div>
  <div class="grid g3">
    ${[
      ['Can we avoid the model call?', 'Exact and semantic caches, guarded so a near-miss is never served as an answer.'],
      ['Can we reduce what we send?', 'Only the context the task actually reads, and compression only where it is measured to be safe.'],
      ['What is the cheapest model that can finish this?', 'Eligibility, expected quality, latency and budget — then the cheapest survivor.'],
      ['How much reasoning is really required?', 'Reasoning level and output budget are policy dimensions, not model defaults.'],
      ['Does this need real-time execution?', 'Work with nobody waiting goes to the discounted lane.'],
      ['Did the answer satisfy the contract?', 'Deterministic validators first. If it failed, escalate — and bill both attempts openly.'],
    ].map(([q, a]) => `<div class="card"><div class="pad">
        <h3 style="margin:0 0 6px;font-size:13.5px">${esc(q)}</h3>
        <div class="note" style="font-size:12px">${esc(a)}</div></div></div>`).join('')}
  </div>
  <div class="callout section" style="margin-top:26px;font-size:14px;line-height:1.75">
    <b>Two things make this different from a router.</b> First, every decision is explainable down to
    the candidates it rejected and why. Second, no savings figure appears anywhere in this product
    without the quality evidence beside it — and where a number is estimated or modelled rather than
    measured, it says so on the same line.
  </div>`;
}

async function waterfallBeat() {
  const d = await all({ wf: '/api/analytics/waterfall' });
  const wf = d.wf?.__error ? null : d.wf;
  if (!wf || !wf.requests) {
    return shell('Where the savings came from', 'Run the earlier steps first.',
      '<div class="card"><div class="empty">No traffic recorded in this demo yet.</div></div>');
  }
  return shell('Where the savings came from',
    'Not a percentage. Each layer\'s contribution, and what each one rests on.',
    `<div class="grid g4">
      ${stat({ label: 'Baseline', value: money(wf.baseline_usd) })}
      ${stat({ label: 'Actual', value: money(wf.final_usd), tone: 'good' })}
      ${stat({ label: 'Saved', value: pct(wf.savings_pct), hero: true, sub: money(wf.savings_usd) })}
      ${stat({ label: 'Bars reconcile', value: wf.reconciles ? 'exactly' : 'NO',
        tone: wf.reconciles ? 'good' : 'risk', sub: 'summed from per-request ledger rows' })}
    </div>
    <div class="section">${card('', waterfall(wf.steps, wf.baseline_usd, wf.final_usd), { pad: true })}</div>
    <div class="section">${card('', table([
      { label: 'Layer', cell: (s) => `<b>${esc(s.label)}</b>` },
      { label: 'Amount', align: 'r', cell: (s) =>
        `<b class="num" style="color:${s.usd >= 0 ? 'var(--save)' : 'var(--risk)'}">
          ${s.usd >= 0 ? '−' : '+'}${money(Math.abs(s.usd))}</b>` },
      { label: 'Requests', align: 'r', cell: (s) => `<span class="num">${int(s.requests)}</span>` },
      { label: 'Basis', cell: (s) => s.basis.map(basisChip).join(' ') },
    ], wf.steps, { minWidth: 620 }), { pad: false })}</div>`);
}

function close() {
  return `<div class="page-head"><div>
    <div class="kicker">Step ${BEATS.length} of ${BEATS.length}</div>
    <h1 style="font-size:var(--t-hero);max-width:24ch">We do not ask you to trust a savings claim.</h1>
    <p class="lede" style="font-size:16px;max-width:68ch">We replay your own traffic through the
      pipeline and show you the receipt, with the quality evidence next to it.</p>
  </div></div>
  <div class="grid g2">
    ${card('What you just saw', `<ol style="margin:0;padding-left:18px;font-size:13px;line-height:2;color:var(--ink-2)">
      <li>Every request routed on what it needed, not on a default.</li>
      <li>Repeat traffic answered without a model call — and a near-miss refused.</li>
      <li>A weak answer caught by the gate and escalated, with both attempts billed.</li>
      <li>A provider failure absorbed without losing the answer.</li>
      <li>A budget limit changing the route, and then rejecting a request outright.</li>
      <li>Every dollar of the difference attributed to a named layer.</li>
    </ol>`)}
    ${card('What happens next', `<ol style="margin:0;padding-left:18px;font-size:13px;line-height:2;color:var(--ink-2)">
      <li><b>Shadow mode on your traffic.</b> Nothing reroutes. You get this report on your own
        query distribution.</li>
      <li><b>Evaluation on your tasks.</b> Assumed quality becomes measured quality, or we find out
        the cheap model is not good enough and say so.</li>
      <li><b>One application, live,</b> behind a quality gate and a budget, reversible with one
        policy change.</li>
    </ol>
    <div class="callout warn" style="margin-top:16px">Every figure in this demo came from a synthetic
      IT-operations workload running against real provider APIs at real published prices. It is a
      measurement of this workload, not a forecast of yours.</div>`)}
  </div>`;
}
