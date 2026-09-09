import { all, post } from '../api.js';
import { card, chip, empty, esc, int, money, ms, pct, stat, stackBar, table, tierChip, toast, STRAT_SHORT } from '../ui.js';
import { dualLine, scatter, tierColor } from '../charts.js';
import { go } from '../main.js';

export async function render(root, state) {
  const d = await all({
    ov: '/api/analytics/overview',
    wf: '/api/analytics/waterfall',
    routing: '/api/analytics/routing',
    cache: '/api/analytics/cache',
    reqs: '/api/requests?limit=400',
  });
  const ov = d.ov?.__error ? null : d.ov;
  if (!ov || !ov.requests) {
    root.innerHTML = head(state) + card('', empty(
      'No traffic recorded yet',
      `<div style="max-width:56ch;margin:0 auto">Run the demo workload to populate every view in this
       app with real measurements. Nothing here is hard-coded: if you clear the request log, these
       panels go empty again.</div>`,
      `<div class="btn-row" style="justify-content:center">
        <button class="btn primary" id="seed">Run the demo workload</button>
        <button class="btn" data-go="present">Open presentation mode</button>
        <button class="btn" data-go="console">Send a single request</button>
      </div>`), { pad: false });
    const b = root.querySelector('#seed');
    if (b) b.onclick = () => runSeed(b, root, state);
    return;
  }

  const wf = d.wf?.__error ? null : d.wf;
  const routing = d.routing?.__error ? null : d.routing;
  const cache = d.cache?.__error ? null : d.cache;
  const reqs = (d.reqs?.requests || []).slice().reverse();
  const strategies = (ov.strategies || []).filter((s) => s.requests);

  // Cumulative baseline vs actual, in request order: the "cost meter" of the demo.
  let a = 0, b = 0;
  const baseSeries = [], actSeries = [];
  reqs.forEach((r) => { a += r.baseline_cost_usd || 0; b += r.cost_usd || 0; baseSeries.push(a); actSeries.push(b); });

  const mix = Object.entries(routing?.by_model || []).length
    ? routing.by_model.map((m) => ({ label: m.model, value: m.requests,
        color: tierColor(m.model.includes('cache') ? 'cache' : (m.tier || 'cache')) }))
    : [];

  root.innerHTML = head(state) + `
  <div class="grid g4">
    ${stat({ label: 'Savings vs always-frontier', value: `${pct(ov.savings_pct)}`, hero: true,
      sub: `${money(ov.baseline_spend_usd)} baseline → ${money(ov.spend_usd)} actual · ${int(ov.requests)} requests` })}
    ${stat({ label: 'Cost per successful task', value: money(ov.cost_per_successful_task_usd),
      sub: `${int(ov.successful_tasks)} of ${int(ov.requests)} passed their quality gate`,
      hint: 'Total spend divided by requests that passed the quality gate. This is the unit that matters, not cost per token.' })}
    ${stat({ label: 'Quality', value: ov.quality_avg ?? '—',
      tone: ov.quality_avg >= 4 ? 'good' : (ov.quality_avg ? 'warn' : ''),
      sub: `${pct(ov.quality_pass_rate, 0)} gate pass rate · ${int(ov.quality_evaluated)} graded` })}
    ${stat({ label: 'P95 latency', value: ms(ov.p95_latency_ms),
      sub: `p50 ${ms(ov.p50_latency_ms)} · ${pct(ov.cache_hit_rate, 0)} served from cache` })}
  </div>

  <div class="grid g-2-1 section">
    ${card('Spend as the workload runs', dualLine(baseSeries, actSeries, {
      labelA: 'Always frontier', labelB: 'Optimized' }) + `
      <div class="note" style="margin-top:10px">Cumulative cost across ${int(reqs.length)} recorded
      requests, in the order they ran. The upper line is what the same traffic would have cost on
      ${esc(state.config?.tiers?.premium?.name || 'the frontier model')};
      ${ov.baseline_source_mix?.measured
        ? `${int(ov.baseline_source_mix.measured)} of those baselines are measured from a real run, the rest estimated from the price registry.`
        : 'baselines are estimated from the versioned price registry.'}</div>`,
      { sub: `${int(reqs.length)} requests` })}
    ${card('Where the traffic went', mix.length ? stackBar(mix) + `
      <div class="note" style="margin-top:14px">
        Frontier share <b>${pct(ov.frontier_pct, 0)}</b> · escalations <b>${pct(ov.escalation_rate, 0)}</b>
        · fallbacks <b>${pct(ov.fallback_rate, 0)}</b>
      </div>` : '<div class="empty">No routing recorded.</div>')}
  </div>

  <div class="grid g3 section">
    ${card('Savings by layer', wf?.steps?.length ? `
      ${wf.steps.slice(0, 6).map((s) => `
        <div style="margin-bottom:11px">
          <div style="display:flex;justify-content:space-between;font-size:12px;align-items:baseline">
            <span>${esc(s.label)}</span>
            <b class="num" style="color:${s.usd >= 0 ? 'var(--save)' : 'var(--risk)'}">
              ${s.usd >= 0 ? '−' : '+'}${money(Math.abs(s.usd))}</b>
          </div>
          <div class="bar" style="margin-top:4px"><div style="width:${Math.min(100, Math.abs(s.pct_of_baseline) * 2)}%;
            background:${s.usd >= 0 ? 'var(--save)' : 'var(--risk)'}"></div></div>
        </div>`).join('')}
      <button class="btn sm" data-go="waterfall" style="margin-top:6px">Open the full waterfall</button>`
      : '<div class="empty">No attribution yet.</div>')}
    ${card('Cost against quality', scatter(strategies.map((s) => ({
        x: s.avg_cost_usd, y: s.avg_quality, label: STRAT_SHORT[s.strategy] || s.strategy, size: s.requests,
        color: s.strategy === 'none' ? 'var(--spend)' : (s.strategy === 'optimized' ? 'var(--save)' : 'var(--warn)'),
      })), { width: 460, height: 250 }) + `<div class="note" style="margin-top:8px">
      One point per strategy on the identical query set. Lower and higher is better.</div>`)}
    ${card('Cache', cache ? `
      <div class="grid g2" style="gap:10px">
        ${stat({ label: 'Exact hit rate', value: pct(cache.exact_hit_rate, 0) })}
        ${stat({ label: 'Semantic hit rate', value: pct(cache.semantic_hit_rate, 0) })}
      </div>
      <div class="note" style="margin-top:12px">
        Cost avoided <b>${money(cache.cost_avoided_usd)}</b>, embedding cost
        <b>${money(cache.embedding_cost_usd)}</b>, net <b>${money(cache.net_cache_benefit_usd)}</b>.
        Hit rate and money saved are reported separately on purpose: a high hit rate on cheap
        requests saves very little.
      </div>
      ${cache.separation?.separable_on_similarity_alone === false ? `
        <div class="callout warn" style="margin-top:12px">
          On this corpus, embedding similarity <b>alone</b> cannot separate a true paraphrase
          (min ${cache.separation.min_paraphrase_similarity}) from a different question
          (max ${cache.separation.max_distractor_similarity}). Subject guards bring the worst
          distractor down to ${cache.separation.max_distractor_similarity_after_guards}, which is
          what makes serving safe.
        </div>` : ''}` : '<div class="empty">No cache activity.</div>')}
  </div>

  <div class="section">
    <div class="head"><div><h2>Strategy comparison</h2>
      <p>The same questions through each strategy. Savings are per-request averages, so an uneven
      number of records cannot flatter a strategy.</p></div>
      <button class="btn sm" data-go="workbench">Evaluation workbench</button></div>
    ${card('', strategies.length ? table([
      { label: 'Strategy', cell: (s) => `<b>${esc(s.label)}</b>` },
      { label: 'Requests', align: 'r', cell: (s) => `<span class="num">${int(s.requests)}</span>` },
      { label: 'Total', align: 'r', cell: (s) => `<b class="num">${money(s.total_cost_usd)}</b>` },
      { label: 'Per request', align: 'r', cell: (s) => `<span class="num">${money(s.avg_cost_usd)}</span>` },
      { label: 'Per solved task', align: 'r', cell: (s) => `<span class="num">${money(s.cost_per_successful_task_usd)}</span>` },
      { label: 'Quality', align: 'r', cell: (s) => `<span class="num">${s.avg_quality ?? '—'}</span>` },
      { label: 'Gate pass', align: 'r', cell: (s) => `<span class="num">${pct(s.quality_pass_rate, 0)}</span>` },
      { label: 'Frontier', align: 'r', cell: (s) => `<span class="num">${pct(s.frontier_pct, 0)}</span>` },
      { label: 'P95', align: 'r', cell: (s) => `<span class="num">${ms(s.p95_latency_ms)}</span>` },
      { label: 'Saving', align: 'r', cell: (s) => s.strategy === 'none' ? '<span class="note">baseline</span>'
        : `<b class="num" style="color:var(--save)">${pct(s.cost_savings_pct)}</b>` },
    ], strategies, { minWidth: 860 }) : empty('Run the workbench to compare strategies'), { pad: false })}
  </div>

  ${budgetSection(ov)}
  ${ov.legacy_requests ? `<div class="note" style="margin-top:18px">
    ${int(ov.legacy_requests)} record(s) in the store predate the cost ledger and carry no baseline or
    attribution, so they are excluded from every figure on this page rather than being counted as
    free. They are still visible in the request log.</div>` : ''}`;

  const seed = root.querySelector('#seed-more');
  if (seed) seed.onclick = () => runSeed(seed, root, state);
}

function head(state) {
  const c = state.config || {};
  return `<div class="page-head">
    <div>
      <div class="kicker">AI FinOps control plane
        ${c.engine_version ? `<span class="chip">${esc(c.engine_version)}</span>` : ''}</div>
      <h1>What is our AI actually costing, and why?</h1>
      <p class="lede">Every figure below is computed from stored request records. Baselines are
        marked measured or estimated, modelled scenarios say so, and clearing the request log
        empties this page.</p>
    </div>
    <div class="btn-row">
      <button class="btn" id="seed-more">Run demo workload</button>
      <button class="btn primary" data-go="present">Presentation mode</button>
    </div>
  </div>`;
}

function budgetSection(ov) {
  const b = (ov.budgets || []).filter((x) => x.limit_usd);
  if (!b.length) {
    return `<div class="section">${card('Budgets',
      `<div class="note">No budget limits are configured for the applications that have run traffic.
       Set one on the <a href="#policies">policies page</a> to see budget pressure change the routing.</div>`)}</div>`;
  }
  return `<div class="section"><div class="head"><div><h2>Budget</h2>
    <p>Pressure is spend against limit for the tightest scope. Above the soft threshold the router
    prefers cheaper eligible routes; a hard limit rejects the request before any model is called.</p>
    </div><button class="btn sm" data-go="policies">Manage budgets</button></div>
    ${card('', table([
      { label: 'Scope', cell: (x) => `<span class="mono" style="font-size:11.5px">${esc(x.scope)}</span>` },
      { label: 'Window', cell: (x) => chip(x.window) },
      { label: 'Limit', align: 'r', cell: (x) => `<span class="num">${money(x.limit_usd)}</span>` },
      { label: 'Spent', align: 'r', cell: (x) => `<span class="num">${money(x.spent_usd)}</span>` },
      { label: 'Remaining', align: 'r', cell: (x) => `<span class="num">${money(x.remaining_usd)}</span>` },
      { label: 'Pressure', cell: (x) => `
        <div style="display:flex;align-items:center;gap:9px">
          <div class="bar" style="width:110px"><div style="width:${Math.min(100, x.pressure * 100)}%;
            background:${x.pressure > 0.8 ? 'var(--risk)' : x.pressure > 0.5 ? 'var(--warn)' : 'var(--save)'}"></div></div>
          <b class="num" style="font-size:11.5px">${pct(x.pressure * 100, 0)}</b></div>` },
    ], b, { minWidth: 640 }), { pad: false })}</div>`;
}

async function runSeed(btn, root, state) {
  btn.disabled = true;
  const original = btn.textContent;
  btn.innerHTML = '<span class="spin"></span> Running baseline…';
  try {
    await post('/api/demo/scenario/baseline');
    btn.innerHTML = '<span class="spin"></span> Running optimizer…';
    await post('/api/demo/scenario/optimized');
    btn.innerHTML = '<span class="spin"></span> Repeating traffic…';
    await post('/api/demo/scenario/cache');
    toast('Demo workload complete — every panel below is now measured from those requests.');
    go('overview');
  } catch (e) {
    toast(`Could not run the workload: ${e.message}`);
    btn.disabled = false;
    btn.textContent = original;
  }
}
