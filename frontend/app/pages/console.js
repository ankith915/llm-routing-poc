// A conversation view with the routing decision shown before every answer.
import { get, post } from '../api.js';
import { card, chip, esc, money, ms, pct, tierChip, toast, truncate } from '../ui.js';
import { openTrace } from './trace.js';

let thread = [];
let samples = [];

export async function render(root, state) {
  if (!samples.length) {
    try { samples = await get('/api/testqueries'); } catch { samples = []; }
  }
  const strategy = state.params.strategy || 'optimized';
  root.innerHTML = `
  <div class="page-head">
    <div><div class="kicker">Query console</div>
      <h1>Ask, and see the decision before the answer</h1>
      <p class="lede">The same endpoint an application would call. Switch the strategy to show the
        identical question landing on different models, and repeat a question to trip the cache.</p></div>
    <div class="btn-row">
      <div class="seg">${['optimized', 'none', 'rule', 'intelligent'].map((s) =>
        `<button class="${strategy === s ? 'on' : ''}" data-strat="${s}">${
          { optimized: 'Optimized', none: 'Baseline', rule: 'Rules', intelligent: 'LLM router' }[s]}</button>`).join('')}</div>
      ${thread.length ? '<button class="btn ghost sm" id="clear">Clear</button>' : ''}
    </div>
  </div>
  <div style="max-width:900px">
    <div id="thread">${thread.length ? thread.map(bubble).join('')
      : `<div class="card"><div class="empty">
          <div class="big">Nothing asked yet</div>
          Ask an IT operations question. The router's decision appears above each answer, with the
          cost it avoided and the quality it was graded at.</div></div>`}</div>
    <div class="card" style="margin-top:14px"><div class="pad-sm">
      <div style="display:flex;gap:10px">
        <textarea id="q" rows="2" placeholder="e.g. Why is payment-api experiencing high latency?"></textarea>
        <button class="btn primary" id="send" style="align-self:stretch">Send</button>
      </div>
      <div class="btn-row" style="margin-top:9px">
        ${['SIMPLE', 'MEDIUM', 'HARD'].map((c) =>
          `<button class="btn ghost sm" data-sample="${c}">try ${c.toLowerCase()}</button>`).join('')}
        <span style="flex:1"></span>
        <span class="note">Enter to send · Shift+Enter for a new line</span>
      </div>
    </div></div>
  </div>`;

  const ta = root.querySelector('#q');
  ta.onkeydown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(root, strategy); }
  };
  root.querySelector('#send').onclick = () => send(root, strategy);
  root.querySelectorAll('[data-strat]').forEach((b) => {
    b.onclick = () => window.go('console', { strategy: b.dataset.strat });
  });
  root.querySelectorAll('[data-sample]').forEach((b) => {
    b.onclick = () => {
      const pool = samples.filter((s) => s.complexity === b.dataset.sample);
      if (pool.length) { ta.value = pool[Math.floor(Math.random() * pool.length)].query; ta.focus(); }
    };
  });
  const clear = root.querySelector('#clear');
  if (clear) clear.onclick = () => { thread = []; window.go('console', { strategy }); };
  root.querySelectorAll('[data-trace]').forEach((b) => {
    b.onclick = () => openTrace(b.dataset.trace);
  });
  const log = root.querySelector('#thread');
  if (log && log.lastElementChild) log.lastElementChild.scrollIntoView({ block: 'end' });
  ta.focus();
}

async function send(root, strategy) {
  const ta = root.querySelector('#q');
  const q = ta.value.trim();
  if (!q) return;
  const btn = root.querySelector('#send');
  btn.disabled = true;
  thread.push({ role: 'user', text: q });
  thread.push({ role: 'pending' });
  root.querySelector('#thread').innerHTML = thread.map(bubble).join('');
  ta.value = '';
  try {
    const r = await post('/api/query', { query: q, strategy, evaluate: true });
    thread[thread.length - 1] = { role: 'ai', r, strategy };
  } catch (e) {
    thread[thread.length - 1] = { role: 'error', text: e.message };
    toast(`That request could not be answered: ${e.message}`);
  }
  btn.disabled = false;
  window.go('console', { strategy });
}

function bubble(m) {
  if (m.role === 'user') {
    return `<div style="display:flex;justify-content:flex-end;margin-bottom:12px">
      <div style="background:var(--hero-bg);color:var(--hero-ink);border-radius:12px 12px 3px 12px;
        padding:10px 15px;font-size:13px;max-width:74%">${esc(m.text)}</div></div>`;
  }
  if (m.role === 'pending') {
    return `<div class="card" style="margin-bottom:12px"><div class="pad-sm">
      <span class="spin"></span> <span class="note">Classifying, checking the cache, routing…</span>
    </div></div>`;
  }
  if (m.role === 'error') {
    return `<div class="card" style="margin-bottom:12px"><div class="pad-sm">
      <div class="callout risk">${esc(m.text)}</div></div></div>`;
  }
  const r = m.r;
  const c = r.classification || {};
  return `<div class="card" style="margin-bottom:12px">
    <div class="title" style="background:var(--surface-2)">
      <div style="display:flex;gap:7px;align-items:center;flex-wrap:wrap">
        ${r.cache_kind ? chip(`${r.cache_kind} cache hit`, 'info') : tierChip(r.tier)}
        ${c.task_label ? chip(c.task_label) : ''}
        ${c.difficulty ? chip(c.difficulty) : ''}
        ${c.rung ? chip(`via ${c.rung}`, c.rung === 'llm_router' ? 'warn' : '') : ''}
        ${r.escalated_from ? chip(`escalated from ${r.escalated_from.split('/').pop()}`, 'warn') : ''}
      </div>
      <div class="sub">${esc((r.model || '').split('/').pop() || '')}</div>
    </div>
    <div class="pad-sm">
      <div class="note" style="border-left:2px solid var(--save);padding-left:10px;margin-bottom:11px">
        ${esc((r.routing || {}).reason || 'served from cache')}</div>
      <div style="white-space:pre-wrap;font-size:13px;line-height:1.65">${esc(r.answer || '')}</div>
    </div>
    <div style="display:flex;gap:14px;flex-wrap:wrap;padding:9px 16px;border-top:1px solid var(--line-2);
      background:var(--surface-2);font-family:var(--mono);font-size:10.5px;color:var(--ink-3)">
      <span>${money(r.cost_usd)} of ${money(r.baseline_cost_usd)}</span>
      <span style="color:var(--save)">${pct(r.savings_pct)} saved</span>
      <span>${ms(r.latency_ms)}</span>
      ${r.quality_score != null ? `<span>quality ${r.quality_score}/5</span>` : ''}
      <span style="flex:1"></span>
      <button class="btn ghost sm" data-trace="${esc(r.request_id)}"
        style="padding:2px 8px;font-size:10px">trace</button>
    </div>
  </div>`;
}
