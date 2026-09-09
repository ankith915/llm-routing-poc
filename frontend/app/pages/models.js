import { all } from '../api.js';
import { card, chip, esc, int, money, ms, pct, stat, table, tierChip } from '../ui.js';
import { scatter, tierColor } from '../charts.js';

export async function render(root, state) {
  const d = await all({ models: '/api/models', tasks: '/api/tasks' });
  const reg = d.models;
  if (reg?.__error) { root.innerHTML = `<div class="callout risk">${esc(reg.__error)}</div>`; return; }
  const models = reg.models || [];
  const usage = Object.fromEntries((reg.usage || []).map((u) => [u.model, u]));
  const measured = reg.measured || {};

  // Points: measured quality where we have it, prior otherwise, marked either way.
  const points = models.filter((m) => m.role !== 'auxiliary').map((m) => {
    const m2 = measured[m.id] || {};
    const cells = Object.values(m2);
    const meas = cells.length ? cells.reduce((a, c) => a + c.mean * c.n, 0) / cells.reduce((a, c) => a + c.n, 0) : null;
    const prior = (m.quality_priors.MEDIUM || m.quality_priors.SIMPLE || {}).score;
    const per1k = (m.input_cost_per_1m * 2 + m.output_cost_per_1m * 0.5) / 1000;
    return { x: per1k, y: meas ?? prior, label: m.label.replace(/GPT-?/, ''), tier: m.tier,
      size: usage[m.id]?.requests || 1, measured: meas != null };
  });

  root.innerHTML = `
  <div class="page-head">
    <div><div class="kicker">Model economics · price registry ${esc(reg.price_registry_version)}
      <span class="chip">effective ${esc(reg.effective_date)}</span></div>
      <h1>What each model costs, and what it is actually worth</h1>
      <p class="lede">Cheap is not the same as bad. Quality here is measured from graded requests in
        this system where enough exist, and an explicitly assumed prior otherwise — the source is on
        every row, and the router refuses to route on an assumed prior unless a policy allows it.</p>
    </div>
  </div>

  <div class="grid g-2-1">
    ${card('Cost against quality', scatter(points, {
      width: 620, height: 320,
      xLabel: 'Cost for a 2k-in / 500-out request (USD)',
    }) + `<div class="note" style="margin-top:10px">Bubble size is how often the model has actually
      been used here. Points marked from an assumed prior sit where we <em>guess</em> they sit; run
      the evaluation workbench to replace a guess with a measurement.</div>`)}
    ${card('Where traffic went', (reg.usage || []).length ? (reg.usage || []).slice(0, 8).map((u) => `
      <div style="margin-bottom:11px">
        <div style="display:flex;justify-content:space-between;font-size:12px">
          <span>${esc((u.model || '').split('/').pop())}</span>
          <b class="num">${pct(u.pct, 0)}</b></div>
        <div class="bar" style="margin-top:4px"><div style="width:${u.pct}%;
          background:${tierColor(u.tier || 'cache')}"></div></div>
        <div class="note" style="margin-top:3px">${int(u.requests)} requests · ${money(u.cost_usd)}
          ${u.avg_quality ? ` · quality ${u.avg_quality}` : ''}</div>
      </div>`).join('') : '<div class="empty">No traffic yet.</div>')}
  </div>

  <div class="section">
    <div class="head"><div><h2>Registry</h2>
      <p>Prices are versioned with an effective date and live only here — no price is hard-coded in
        application logic. Change a price and every figure in the product moves with it.</p></div></div>
    ${card('', table([
      { label: 'Model', cell: (m) => `<b>${esc(m.label)}</b>
          <div class="note mono" style="font-size:10px">${esc(m.id)}</div>` },
      { label: 'Tier', cell: (m) => tierChip(m.tier) },
      { label: 'Provider', cell: (m) => esc(m.provider) },
      { label: 'In /1M', align: 'r', cell: (m) => `<span class="num">$${m.input_cost_per_1m}</span>` },
      { label: 'Out /1M', align: 'r', cell: (m) => `<span class="num">$${m.output_cost_per_1m}</span>` },
      { label: 'Cached in', align: 'r', cell: (m) => m.cached_input_cost_per_1m != null
          ? `<span class="num">$${m.cached_input_cost_per_1m}</span>`
          : '<span class="note">n/a</span>' },
      { label: 'Context', align: 'r', cell: (m) => `<span class="num">${int(m.max_context_tokens / 1000)}k</span>` },
      { label: 'p50', align: 'r', cell: (m) => m.latency.p50_ms
          ? `<span class="num">${ms(m.latency.p50_ms)}</span>`
          : '<span class="note">unmeasured</span>' },
      { label: 'Quality by difficulty', cell: (m) => ['SIMPLE', 'MEDIUM', 'HARD'].map((k) => {
          const p = m.quality_priors[k];
          if (!p) return '';
          const meas = (measured[m.id] || {})[k];
          const val = meas ? meas.mean : p.score;
          const src = meas ? `learned n=${meas.n}` : (p.source === 'measured' ? `measured n=${p.n}` : 'assumed');
          return `<span class="chip ${meas || p.source === 'measured' ? 'save' : 'warn'}"
            title="${esc(src)}">${k[0]} ${val}</span>`;
        }).join(' ') },
      { label: 'Capabilities', cell: (m) => Object.entries(m.capabilities)
          .filter(([, v]) => v).map(([k]) => `<span class="chip">${esc(k.replace(/_/g, ' '))}</span>`).join(' ') },
      { label: '', cell: (m) => m.enabled ? '' : chip('disabled', 'warn') },
    ], models, { minWidth: 1240 }), { pad: false })}
    <div class="note" style="margin-top:10px">
      Green quality chips are measurements from graded requests; amber chips are assumed priors that
      nobody has verified on this workload. <code>allow_unmeasured_models</code> is false by default,
      so an amber cell keeps that model out of the routing decision.
    </div>
  </div>

  <div class="section">
    <div class="head"><div><h2>Providers</h2>
      <p>Capability differs by provider and the product does not pretend otherwise. Prompt caching
        and batch discounts are only claimed where the provider publishes them.</p></div></div>
    ${card('', table([
      { label: 'Provider', cell: ([k, p]) => `<b>${esc(p.label || k)}</b>` },
      { label: 'Prompt cache', cell: ([, p]) => p.prompt_cache
          ? chip('yes', 'save') : chip('not claimed') },
      { label: 'Batch discount', cell: ([, p]) => p.batch_discount
          ? chip(`${Math.round(p.batch_discount * 100)}% off`, 'save')
          : chip('none published') },
      { label: 'Note', cell: ([, p]) => `<span class="note">${esc(p.note || '')}</span>` },
    ], Object.entries(reg.providers || {}), { minWidth: 620 }), { pad: false })}
  </div>

  <div class="section">
    <div class="head"><div><h2>Task types</h2>
      <p>Adding a task type is a data change, not a router change. Each declares what it reads, how
        it can be checked deterministically, and how many output tokens it deserves.</p></div></div>
    ${card('', table([
      { label: 'Task', cell: (t) => `<b>${esc(t.label)}</b>
          <div class="note mono" style="font-size:10px">${esc(t.name)}</div>` },
      { label: 'Family', cell: (t) => chip(t.family) },
      { label: 'Floor', cell: (t) => esc(t.default_difficulty) },
      { label: 'Validator', cell: (t) => t.validator
          ? chip(t.validator, 'save') : '<span class="note">judge only</span>' },
      { label: 'Output budget', align: 'r', cell: (t) => `<span class="num">${int(t.output_budget_tokens)}</span>` },
      { label: 'Reads', cell: (t) => (t.context_sections || []).map((s) => `<span class="chip">${esc(s)}</span>`).join(' ')
          || '<span class="note">self-contained</span>' },
      { label: '', cell: (t) => [t.batch_eligible ? chip('batch-eligible', 'info') : '',
          t.high_stakes ? chip('high stakes', 'warn') : ''].join(' ') },
    ], (d.tasks?.tasks) || [], { minWidth: 860 }), { pad: false })}
  </div>`;
}
