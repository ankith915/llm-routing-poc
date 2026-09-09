import { all } from '../api.js';
import { basisChip, card, empty, esc, int, money, pct, stat, table } from '../ui.js';
import { waterfall } from '../charts.js';

const LAYER_NOTE = {
  exact_cache: 'An identical, version-matched request was answered from the store. No model was called.',
  semantic_cache: 'An equivalent request was answered from the store after passing every guard: similarity, subject, task type, versions, freshness and the stored answer’s own quality gate.',
  context: 'Context the task does not read was removed before the prompt was sent. Priced at the premium input rate on the tokens removed.',
  routing: 'The difference between what the frontier model would have cost on this exact prompt and what the selected model actually cost.',
  reasoning: 'Hidden reasoning tokens avoided by lowering the reasoning level, using the registry’s measured token counts. This bar is an estimate.',
  execution_mode: 'The provider’s published batch discount applied to the served call. This demo executes synchronously, so the bar is modelled.',
  provider_cache: 'Prompt tokens the provider served from its own prefix cache, billed at the cached rate instead of the full input rate. Kept separate from the routing bar so neither counts the other.',
  overhead: 'What the optimizer itself cost: the LLM router rung and embedding calls. A negative bar, because it is spend.',
  escalation: 'Attempts the quality gate rejected. Paid for, and shown, rather than hidden.',
  fallback: 'Attempts that failed at the provider before one succeeded.',
};

export async function render(root, state) {
  const d = await all({ wf: '/api/analytics/waterfall', ov: '/api/analytics/overview' });
  const wf = d.wf?.__error ? null : d.wf;
  if (!wf || !wf.requests) {
    root.innerHTML = head() + card('', empty('Nothing to attribute yet',
      'Run some traffic and every dollar of the difference between baseline and actual will be split into bars here.',
      '<button class="btn primary" data-go="overview">Run the demo workload</button>'), { pad: false });
    return;
  }
  const rows = wf.steps.map((s) => ({ ...s, note: LAYER_NOTE[s.layer] || '' }));
  root.innerHTML = head() + `
    <div class="grid g4">
      ${stat({ label: 'Baseline spend', value: money(wf.baseline_usd),
        sub: `every request on the frontier model` })}
      ${stat({ label: 'Actual spend', value: money(wf.final_usd), tone: 'good',
        sub: `${int(wf.requests)} requests` })}
      ${stat({ label: 'Saved', value: money(wf.savings_usd), tone: 'good',
        sub: `${pct(wf.savings_pct)} of baseline` })}
      ${stat({ label: 'Reconciles', value: wf.reconciles ? 'yes' : 'NO',
        tone: wf.reconciles ? 'good' : 'risk',
        sub: wf.reconciles
          ? 'bars sum exactly to the saving'
          : `residual ${money(wf.residual_usd)} — this is a bug, not a rounding artefact`,
        hint: 'The bars are summed from per-request ledger rows, so the total cannot drift from the parts.' })}
    </div>

    <div class="section">
      ${card('Baseline → actual', waterfall(wf.steps, wf.baseline_usd, wf.final_usd) + `
        <div class="note" style="margin-top:12px">
          Green bars are money kept, red bars are money the optimizer spent to keep it. Each bar is
          summed from the per-request ledger, so this chart is an aggregation of measured rows rather
          than a separate calculation that could drift from them.
          ${wf.baseline_source_mix ? `Baselines: ${Object.entries(wf.baseline_source_mix)
            .map(([k, v]) => `${int(v)} ${esc(k)}`).join(', ')}.` : ''}
        </div>`, { sub: `${int(wf.requests)} requests` })}
    </div>

    <div class="section">
      <div class="head"><div><h2>What each bar means</h2>
        <p>The basis column is the honest part: measured comes from real token counts, estimated from
        the price registry, modelled from a published rate applied to a scenario this demo did not
        execute.</p></div></div>
      ${card('', table([
        { label: 'Layer', cell: (r) => `<b>${esc(r.label)}</b>` },
        { label: 'Amount', align: 'r', cell: (r) =>
          `<b class="num" style="color:${r.usd >= 0 ? 'var(--save)' : 'var(--risk)'}">
            ${r.usd >= 0 ? '−' : '+'}${money(Math.abs(r.usd))}</b>` },
        { label: 'Of baseline', align: 'r', cell: (r) => `<span class="num">${pct(Math.abs(r.pct_of_baseline), 2)}</span>` },
        { label: 'Requests', align: 'r', cell: (r) => `<span class="num">${int(r.requests)}</span>` },
        { label: 'Basis', cell: (r) => r.basis.map(basisChip).join(' ') },
        { label: 'What it is', cell: (r) => `<span class="note">${esc(r.note)}</span>` },
      ], rows, { minWidth: 900 }), { pad: false })}
    </div>

    <div class="section">
      ${card('How the baseline is decided', `
        <div class="note" style="line-height:1.75;max-width:82ch">
          In production you cannot run the baseline and the optimised path side by side without
          doubling the bill, so the baseline is reconstructed per request: the frontier model's price
          applied to that request's own uncompressed input tokens and the output length it actually
          produced.
          <br><br>
          Substituting a real baseline run's dollar cost instead would look more honest and measure
          worse. Two runs of the same question on the same model differ mainly in how long the answer
          happens to be, so a single request could show a negative saving purely from that variance.
          Real frontier-model runs are used differently: they <b>calibrate</b> the estimate. The
          ratio of true cost to estimated cost is averaged across those runs and applied to every
          estimate, which removes the bias without importing the variance. A baseline marked
          <b>calibrated</b> has at least three real runs behind that factor; one marked
          <b>estimated</b> does not yet.
        </div>
        <div class="grid g3" style="margin-top:16px">
          ${Object.entries(wf.baseline_source_mix || {}).map(([k, v]) =>
            stat({ label: `${k} baselines`, value: int(v) })).join('')}
        </div>`)}
    </div>`;
}

function head() {
  return `<div class="page-head"><div>
    <div class="kicker">Savings attribution</div>
    <h1>Where the savings came from</h1>
    <p class="lede">Not "we saved 40%". Each layer's contribution, what it rests on, and whether the
      bars add up.</p>
  </div>
  <div class="btn-row"><button class="btn" data-go="requests">Inspect a single request</button></div></div>`;
}
