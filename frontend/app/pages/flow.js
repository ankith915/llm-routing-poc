// The architecture, lit up by a real request as it moves through it.
import { get, post } from '../api.js';
import { card, chip, esc, int, money, ms, pct, tierChip, toast, tokens } from '../ui.js';
import { openTrace } from './trace.js';
import { later } from '../main.js';

const STAGES = [
  ['policy', 'Policy engine', 'Resolve tenant, application and request policy'],
  ['budget', 'Budget', 'Reserve worst-case spend; reject over a hard limit'],
  ['exact_cache', 'Exact cache', 'Canonical request hash, version-matched'],
  ['classify', 'Task classifier', 'Rules → local classifier → LLM router only if unsure'],
  ['semantic_cache', 'Semantic cache', 'Similarity plus subject, task and version guards'],
  ['route', 'Routing engine', 'Cheapest model meeting quality, latency and policy'],
  ['context', 'Context optimizer', 'Keep what the task reads; tier-aware compression'],
  ['plan', 'Execution planner', 'Mode, reasoning level, output budget'],
  ['execute', 'Provider pool', 'Call, with fallback and circuit breaker'],
  ['quality', 'Quality gate', 'Deterministic validators first, judge only if needed'],
  ['escalate', 'Escalation', 'Retry higher when the gate fails'],
  ['ledger', 'Cost ledger', 'Actual, baseline, and the attribution bars'],
];

let samples = [];

export async function render(root, state) {
  if (!samples.length) {
    try { samples = await get('/api/testqueries'); } catch { samples = []; }
  }
  root.innerHTML = `
  <div class="page-head">
    <div><div class="kicker">Architecture</div>
      <h1>Watch one request move through the control plane</h1>
      <p class="lede">Each stage lights up as the request reaches it, and shows what it actually
        decided. This is the same pipeline that serves every request — not an illustration of it.</p>
    </div>
  </div>

  <div class="grid g-1-2">
    <div>
      ${card('Send a request', `
        <label class="field"><span class="lab">Question</span>
          <textarea id="q" rows="3">Why is payment-api experiencing high latency?</textarea></label>
        <div class="btn-row" style="margin-top:10px">
          ${['SIMPLE', 'MEDIUM', 'HARD'].map((c) =>
            `<button class="btn ghost sm" data-sample="${c}">${c.toLowerCase()} example</button>`).join('')}
        </div>
        <div class="grid g2" style="margin-top:12px;gap:10px">
          <label class="field"><span class="lab">Strategy</span>
            <select id="strategy">
              <option value="optimized">Optimized (full control plane)</option>
              <option value="none">Baseline (always frontier)</option>
              <option value="rule">Rule router only</option>
              <option value="intelligent">LLM router only</option>
            </select></label>
          <label class="field"><span class="lab">Application</span>
            <select id="app">
              <option value="ops-assistant">ops-assistant (interactive)</option>
              <option value="ticket-enrichment">ticket-enrichment (batch)</option>
              <option value="finance-copilot">finance-copilot (confidential)</option>
            </select></label>
        </div>
        <button class="btn primary" id="send" style="margin-top:14px;width:100%">Send through the optimizer</button>
        <div class="note" style="margin-top:10px">Ask the same question twice to watch the cache
          intercept it before the router is ever consulted.</div>`)}
      <div id="verdict" style="margin-top:16px"></div>
    </div>
    ${card('Request pipeline', `<div class="flow" id="flow">${STAGES.map(stageRow).join('')}</div>`,
      { sub: 'idle' })}
  </div>`;

  root.querySelectorAll('[data-sample]').forEach((b) => {
    b.onclick = () => {
      const pool = samples.filter((s) => s.complexity === b.dataset.sample);
      if (pool.length) root.querySelector('#q').value = pool[Math.floor(Math.random() * pool.length)].query;
    };
  });
  root.querySelector('#send').onclick = () => send(root);
}

function stageRow([id, name, desc]) {
  return `<div class="fstage idle" data-stage="${id}">
      <div class="nm">${esc(name)}</div>
      <div class="ds">${esc(desc)}</div>
      <div class="mono" style="font-size:10.5px;color:var(--ink-4)" data-slot="${id}"></div>
    </div>
    <div class="farrow"></div>`;
}

async function send(root) {
  const btn = root.querySelector('#send');
  const query = root.querySelector('#q').value.trim();
  if (!query) return;
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Running…';
  root.querySelector('#verdict').innerHTML = '';
  root.querySelectorAll('.fstage').forEach((el) => {
    el.className = 'fstage idle';
    el.querySelector('[data-slot]').textContent = '';
  });
  try {
    const r = await post('/api/query', {
      query,
      strategy: root.querySelector('#strategy').value,
      application_id: root.querySelector('#app').value,
      evaluate: true,
    });
    await animate(root, r);
    root.querySelector('#verdict').innerHTML = verdict(r);
    root.querySelector('#open-trace').onclick = () => openTrace(r.request_id);
  } catch (e) {
    toast(`That request could not be completed: ${e.message}`);
    root.querySelectorAll('.fstage.idle').forEach((el) => { el.className = 'fstage skipped'; });
  }
  btn.disabled = false;
  btn.textContent = 'Send through the optimizer';
}

/** Replay the recorded trace stage by stage. The delays are presentation, the
 *  content is entirely from the response. */
async function animate(root, r) {
  const byStage = Object.fromEntries((r.trace || []).map((s) => [s.stage, s]));
  for (const [id] of STAGES) {
    const el = root.querySelector(`[data-stage="${id}"]`);
    const s = byStage[id];
    if (!el) continue;
    if (!s) { el.className = 'fstage skipped'; continue; }
    el.className = 'fstage active';
    el.querySelector('.ds').textContent = s.summary;
    el.querySelector('[data-slot]').innerHTML = slot(s);
    el.classList.add('click');
    el.onclick = () => openTrace(r.request_id);
    await sleep(170);
    el.className = `fstage click ${s.status === 'hit' ? 'hit'
      : s.status === 'skipped' ? 'skipped'
      : ['failed', 'rejected'].includes(s.status) ? 'failed' : 'done'}`;
  }
}
const sleep = (ms) => new Promise((res) => later(res, ms));

function slot(s) {
  const bits = [];
  if (s.cost_usd) bits.push(money(s.cost_usd));
  if (s.tokens) bits.push(`${tokens(s.tokens)} tok`);
  if (s.latency_ms) bits.push(ms(s.latency_ms));
  return esc(bits.join(' · '));
}

function verdict(r) {
  const saved = (r.baseline_cost_usd || 0) - (r.cost_usd || 0);
  return card('Result', `
    <div class="grid g2" style="gap:10px">
      <div class="stat"><div class="l">Cost</div><div class="v">${money(r.cost_usd)}</div>
        <div class="n">baseline ${money(r.baseline_cost_usd)}</div></div>
      <div class="stat good"><div class="l">Saved</div><div class="v">${pct(r.savings_pct)}</div>
        <div class="n">${money(saved)}</div></div>
    </div>
    <div class="btn-row" style="margin-top:12px">
      ${r.cache_kind ? chip(`${r.cache_kind} cache`, 'info') : tierChip(r.tier)}
      ${r.quality_score != null ? chip(`quality ${r.quality_score}/5`,
        r.quality_gate_passed === false ? 'risk' : 'save') : ''}
      ${chip(ms(r.latency_ms))}
      ${r.escalated_from ? chip('escalated', 'warn') : ''}
      ${r.fallback_used ? chip('fallback', 'warn') : ''}
    </div>
    <div class="note" style="margin-top:12px">${esc((r.routing || {}).reason || '')}</div>
    <div style="white-space:pre-wrap;font-size:12.5px;line-height:1.6;margin-top:12px;
      padding-top:12px;border-top:1px solid var(--line-2)">${esc(r.answer || '')}</div>
    <button class="btn sm" id="open-trace" style="margin-top:12px">Open the full trace</button>`);
}
