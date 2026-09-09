import { all } from '../api.js';
import { card, chip, empty, esc, int, money, pct, stat, table, truncate } from '../ui.js';
import { openTrace } from './trace.js';
import { ranked } from '../charts.js';

export async function render(root, state) {
  const d = await all({ q: '/api/analytics/quality', clf: '/api/classifier', cache: '/api/analytics/cache' });
  const q = d.q?.__error ? null : d.q;
  if (!q || !q.evaluated) {
    root.innerHTML = head() + card('', empty('Nothing graded yet',
      'Run the workload with evaluation on and this page fills with validator outcomes, judge scores and every request the gate rejected.',
      '<button class="btn primary" data-go="overview">Run the demo workload</button>'), { pad: false });
    return;
  }
  const validators = Object.entries(q.validators || {});
  root.innerHTML = head() + `
    <div class="grid g4">
      ${stat({ label: 'Gate pass rate', value: pct(q.gate_pass_rate, 0),
        tone: q.gate_pass_rate >= 90 ? 'good' : 'warn', sub: `${int(q.evaluated)} graded requests` })}
      ${stat({ label: 'Gate failures', value: int(q.gate_failures.length),
        tone: q.gate_failures.length ? 'warn' : 'good', sub: 'answers the gate refused' })}
      ${stat({ label: 'Escalations', value: int(q.escalations.length),
        sub: 'failed cheap attempt, retried higher' })}
      ${stat({ label: 'Deterministic checks', value: int(validators.reduce((a, [, v]) => a + v.pass + v.fail, 0)),
        sub: 'free, exact, no judge needed' })}
    </div>

    <div class="grid g2 section">
      ${card('Checks that ran', validators.length ? table([
        { label: 'Validator', cell: ([k]) => `<b>${esc(k)}</b>
            <div class="note">${esc({
              label: 'exact label match against the allowed set',
              json_schema: 'parses and validates against the declared schema',
              sql: 'executed against the demo warehouse and compared with a reference result',
              groundedness: 'every number and entity in the answer must appear in the context',
              non_empty: 'the model returned something',
            }[k] || '')}</div>` },
        { label: 'Passed', align: 'r', cell: ([, v]) => `<span class="num" style="color:var(--save)">${int(v.pass)}</span>` },
        { label: 'Failed', align: 'r', cell: ([, v]) => `<span class="num" style="color:${v.fail ? 'var(--risk)' : 'var(--ink-3)'}">${int(v.fail)}</span>` },
      ], validators, { minWidth: 420 }) + `<div class="note" style="margin-top:10px">${esc(q.judge_cost_note)}</div>`
        : '<div class="empty">No validators ran.</div>')}
      ${card('Quality by model', ranked(Object.entries(q.by_model || {})
        .sort((a, b) => b[1].avg_quality - a[1].avg_quality)
        .map(([m, v]) => ({ label: m.split('/').pop(), value: v.avg_quality,
          sub: `${int(v.evaluated)} graded`,
          color: v.avg_quality >= 4.3 ? 'var(--save)' : v.avg_quality >= 3.8 ? 'var(--warn)' : 'var(--risk)' })),
        { valueFmt: (v) => v.toFixed(2) }))}
    </div>

    <div class="grid g2 section">
      ${card('Quality by task', ranked(Object.entries(q.by_task || {})
        .sort((a, b) => a[1].avg_quality - b[1].avg_quality).slice(0, 8)
        .map(([t, v]) => ({ label: t.replace(/_/g, ' '), value: v.avg_quality,
          sub: `${int(v.evaluated)} graded`,
          color: v.avg_quality >= 4.3 ? 'var(--save)' : 'var(--warn)' })),
        { valueFmt: (v) => v.toFixed(2) }))}
      ${card('Score distribution', Object.entries(q.score_distribution || {}).map(([s, n]) => `
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:7px">
          <span class="mono" style="width:14px">${esc(s)}</span>
          <div class="bar" style="flex:1"><div style="width:${(100 * n) / Math.max(1,
            Math.max(...Object.values(q.score_distribution)))}%;background:${Number(s) >= 4 ? 'var(--save)'
            : Number(s) === 3 ? 'var(--warn)' : 'var(--risk)'}"></div></div>
          <b class="num" style="width:28px;text-align:right">${int(n)}</b>
        </div>`).join(''))}
    </div>

    ${q.gate_failures.length ? `<div class="section">
      <div class="head"><div><h2>Where the cheap route was wrong</h2>
        <p>These are the requests the gate refused. They are the reason the savings can be trusted:
          the system found them, not the client.</p></div></div>
      ${card('', table([
        { label: 'Request', cell: (r) => `<div>${esc(truncate(r.query, 74))}</div>
            <div class="note mono" style="font-size:10px">${esc(r.request_id)}</div>` },
        { label: 'Model', cell: (r) => esc((r.escalated_from || r.model || '').split('/').pop()) },
        { label: 'Why it failed', cell: (r) => `<span class="note">${esc(r.reason)}</span>` },
        { label: 'Then', cell: (r) => r.escalated_to
            ? chip(`escalated to ${(r.escalated_to || '').split('/').pop()}`, 'save')
            : chip('served as-is', 'warn') },
      ], q.gate_failures, { onRow: true, minWidth: 760 }), { pad: false })}
    </div>` : ''}

    ${q.escalations.length ? `<div class="section">
      ${card('Escalations', table([
        { label: 'Request', cell: (r) => `<div>${esc(truncate(r.query, 66))}</div>
            <div class="note mono" style="font-size:10px">${esc(r.request_id)}</div>` },
        { label: 'From', cell: (r) => esc((r.from || '').split('/').pop()) },
        { label: 'To', cell: (r) => esc((r.to || '').split('/').pop()) },
        { label: 'Extra cost', align: 'r', cell: (r) => `<span class="num">${money(r.extra_cost_usd)}</span>` },
        { label: 'Final score', align: 'r', cell: (r) => `<span class="num">${r.final_score ?? '—'}</span>` },
      ], q.escalations, { onRow: true, minWidth: 700 }), { pad: false })}
      <div class="note" style="margin-top:9px">Both attempts are billed and both appear in the
        ledger. An escalation is the safety net working, and it is shown as a cost, not hidden.</div>
    </div>` : ''}

    <div class="section">
      <div class="head"><div><h2>The router that made these decisions</h2>
        <p>Leave-one-out accuracy: each labelled query is predicted by a classifier trained without
          it. This is the honest number, not training accuracy.</p></div></div>
      <div class="grid g3">
        ${stat({ label: 'Task type accuracy', value: pct((d.clf?.accuracy?.task_type_accuracy || 0) * 100, 1),
          sub: `${int(d.clf?.accuracy?.n)} labelled queries` })}
        ${stat({ label: 'Difficulty accuracy', value: pct((d.clf?.accuracy?.difficulty_accuracy || 0) * 100, 1) })}
        ${stat({ label: 'Trained on', value: int((d.clf?.trained_on?.labelled || 0) + (d.clf?.trained_on?.logged || 0)),
          sub: `${int(d.clf?.trained_on?.logged)} distilled from LLM router decisions` })}
      </div>
    </div>`;

  root.querySelectorAll('tbody tr[data-row]').forEach((tr, i) => {
    const all = [...root.querySelectorAll('tbody tr[data-row]')];
    void all;
    const id = tr.querySelector('.mono')?.textContent?.trim();
    if (id) tr.onclick = () => openTrace(id);
  });
}

function head() {
  return `<div class="page-head"><div>
    <div class="kicker">Quality gate</div>
    <h1>Is the quality safe?</h1>
    <p class="lede">Deterministic validators run first and cost nothing: an exact label, a schema,
      an executed SQL query, a groundedness check. The LLM judge runs only where no contract exists.
      Cost savings are never shown without this page behind them.</p>
  </div></div>`;
}
