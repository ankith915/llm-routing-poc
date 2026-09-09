import { get } from '../api.js';
import { card, chip, empty, esc, int, money, ms, pct, stat, table, tierChip, truncate, ago } from '../ui.js';
import { openTrace } from './trace.js';
import { go } from '../main.js';

const FILTERS = [
  ['', 'All'], ['optimized', 'Optimized'], ['none', 'Baseline'],
  ['rule', 'Rule router'], ['intelligent', 'LLM router'], ['shadow', 'Shadow'],
];

export async function render(root, state) {
  const strategy = state.params.strategy || '';
  const q = (state.params.q || '').toLowerCase();
  const data = await get(`/api/requests?limit=500${strategy ? `&strategy=${strategy}` : ''}`);
  let rows = data.requests || [];
  if (q) rows = rows.filter((r) => (r.query || '').toLowerCase().includes(q)
    || (r.model || '').toLowerCase().includes(q) || (r.request_id || '').toLowerCase().includes(q));

  const cost = rows.reduce((a, r) => a + (r.cost_usd || 0), 0);
  const base = rows.reduce((a, r) => a + (r.baseline_cost_usd || 0), 0);
  const graded = rows.filter((r) => r.quality_score != null);

  root.innerHTML = `
  <div class="page-head">
    <div><div class="kicker">Request log</div>
      <h1>Every request, and why it cost what it cost</h1>
      <p class="lede">Click any row for the full trace: the policy that applied, what the router
        considered, what it rejected and what the answer was graded at.</p></div>
    <div class="btn-row">
      <input type="text" id="q" placeholder="Filter by query, model or id" value="${esc(state.params.q || '')}"
        style="width:230px">
      <button class="btn" data-go="console">Send a request</button>
    </div>
  </div>

  ${rows.length ? `<div class="grid g4">
    ${stat({ label: 'Requests', value: int(rows.length) })}
    ${stat({ label: 'Spend', value: money(cost), sub: `against ${money(base)} baseline` })}
    ${stat({ label: 'Saved', value: pct(base ? (100 * (base - cost)) / base : 0), tone: 'good',
      sub: money(base - cost) })}
    ${stat({ label: 'Graded', value: `${int(graded.length)}`,
      sub: graded.length ? `mean ${(graded.reduce((a, r) => a + r.quality_score, 0) / graded.length).toFixed(2)}` : 'none yet' })}
  </div>` : ''}

  <div class="section">
    <div class="seg" style="margin-bottom:12px">
      ${FILTERS.map(([v, l]) => `<button class="${strategy === v ? 'on' : ''}" data-filter="${v}">${esc(l)}</button>`).join('')}
    </div>
    ${card('', rows.length ? table([
      { label: 'Request', cell: (r) => `<div style="min-width:0">
          <div style="font-size:12.5px">${esc(truncate(r.query, 68))}</div>
          <div class="note mono" style="font-size:10px">${esc(r.request_id)} · ${ago(r.timestamp)}
            · ${esc(r.application_id || '')}</div></div>` },
      { label: 'Task', cell: (r) => r.task_label
          ? `${chip(r.task_label)}<div class="note" style="margin-top:2px">${esc(r.difficulty || '')}</div>` : '—' },
      { label: 'Routed to', cell: (r) => r.rejected ? chip('rejected', 'risk')
          : `${tierChip(r.cache_kind ? `${r.cache_kind} cache` : r.tier)}
             <div class="note mono" style="margin-top:2px;font-size:10px">${esc((r.model || '').split('/').pop() || '')}</div>` },
      { label: 'Cost', align: 'r', cell: (r) => `<b class="num">${money(r.cost_usd)}</b>
          <div class="note num" style="font-size:10px">of ${money(r.baseline_cost_usd)}</div>` },
      { label: 'Saved', align: 'r', cell: (r) => r.savings_pct != null
          ? `<span class="num" style="color:${r.savings_pct > 0 ? 'var(--save)' : 'var(--ink-3)'}">${pct(r.savings_pct, 0)}</span>` : '—' },
      { label: 'Quality', align: 'r', cell: (r) => r.quality_score != null
          ? `<span class="num" style="color:${r.quality_gate_passed === false ? 'var(--risk)'
             : r.quality_score >= 4 ? 'var(--save)' : 'var(--warn)'}">${r.quality_score}</span>` : '—' },
      { label: 'Latency', align: 'r', cell: (r) => `<span class="num">${ms(r.latency_ms)}</span>` },
      { label: '', cell: (r) => [
          r.escalated_from ? chip('escalated', 'warn') : '',
          r.fallback_used ? chip('fallback', 'warn') : '',
          r.mode === 'shadow' ? chip('shadow', 'info') : '',
        ].join(' ') },
    ], rows, { onRow: true, minWidth: 940 })
      : empty('No requests match', 'Clear the filter, or run the demo workload from the overview.',
              '<button class="btn primary" data-go="overview">Go to overview</button>'), { pad: false })}
  </div>`;

  root.querySelectorAll('[data-filter]').forEach((b) => {
    b.onclick = () => go('requests', { ...state.params, strategy: b.dataset.filter || undefined });
  });
  root.querySelectorAll('tbody tr[data-row]').forEach((tr) => {
    tr.onclick = () => openTrace(rows[Number(tr.dataset.row)].request_id);
  });
  const input = root.querySelector('#q');
  if (input) {
    let t;
    input.oninput = () => {
      clearTimeout(t);
      t = setTimeout(() => go('requests', { ...state.params, q: input.value || undefined }), 300);
    };
  }
}
