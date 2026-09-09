import { all, post } from '../api.js';
import { basisChip, card, chip, empty, esc, int, money, pct, stat, table, toast, truncate } from '../ui.js';
import { openTrace } from './trace.js';
import { go } from '../main.js';

export async function render(root, state) {
  const d = await all({ s: '/api/analytics/shadow' });
  const s = d.s?.__error ? null : d.s;
  root.innerHTML = `
  <div class="page-head">
    <div><div class="kicker">Shadow mode</div>
      <h1>See the saving before changing anything</h1>
      <p class="lede">The application keeps calling the model it uses today. The optimizer watches
        the same traffic, decides what it <em>would</em> have done, and prices that decision from the
        versioned registry using the incumbent's own token counts. No production request is rerouted.</p></div>
    <div class="btn-row"><button class="btn primary" id="run">Run shadow traffic</button></div>
  </div>

  ${!s || !s.requests ? card('', empty('No shadow traffic yet',
      `Shadow mode is the safe adoption path: a client points a copy of their traffic at the
       optimizer and gets a report, without a single request changing route.`,
      '<button class="btn primary" id="run2">Run shadow traffic</button>'), { pad: false })
    : report(s)}`;

  [root.querySelector('#run'), root.querySelector('#run2')].forEach((b) => {
    if (b) b.onclick = () => run(b);
  });
  root.querySelectorAll('tbody tr[data-row]').forEach((tr) => {
    const id = tr.querySelector('.mono')?.textContent?.trim();
    if (id) tr.onclick = () => openTrace(id);
  });
}

function report(s) {
  const qualityLine = s.projected_avg_quality && s.incumbent_avg_quality
    ? `${s.projected_avg_quality} projected against ${s.incumbent_avg_quality} measured on the incumbent`
    : 'not enough graded requests to compare quality yet';
  return `
    <div class="grid g4">
      ${stat({ label: 'Requests analysed', value: int(s.requests), sub: 'served by the incumbent model' })}
      ${stat({ label: 'Current spend', value: money(s.current_spend_usd), sub: 'measured' })}
      ${stat({ label: 'Optimizer would have spent', value: money(s.projected_spend_usd), tone: 'good',
        sub: 'estimated from the price registry' })}
      ${stat({ label: 'Potential saving', value: pct(s.projected_saving_pct), hero: true,
        sub: `${money(s.projected_saving_usd)} on this sample` })}
    </div>

    <div class="callout warn section" style="margin-top:22px">
      <b>This is an estimate, and the product says so everywhere it appears.</b> ${esc(s.note)}
      Quality: ${esc(qualityLine)}. Turning shadow into live is a policy change, and the evaluation
      workbench is where you would verify it first.
    </div>

    <div class="grid g2 section">
      ${card('What it would have used instead', Object.entries(s.projected_model_mix || {})
        .sort((a, b) => b[1] - a[1]).map(([m, n]) => `
        <div style="margin-bottom:10px">
          <div style="display:flex;justify-content:space-between;font-size:12px">
            <span>${esc(m === 'cache' ? 'served from cache, no model call' : m.split('/').pop())}</span>
            <b class="num">${int(n)}</b></div>
          <div class="bar" style="margin-top:4px"><div style="width:${(100 * n) / s.requests}%;
            background:${m === 'cache' ? 'var(--tier-cache)' : 'var(--save)'}"></div></div>
        </div>`).join('') + `<div class="note" style="margin-top:12px">
          ${int(s.would_cache)} of ${int(s.requests)} would have been answered from cache with no
          model call at all.</div>`)}
      ${card('How a client would adopt this', `
        <ol style="margin:0;padding-left:18px;font-size:12.5px;line-height:1.9;color:var(--ink-2)">
          <li>Mirror a slice of production traffic at the optimizer in shadow mode. Nothing changes.</li>
          <li>Read this report after a week of real traffic, on their own query distribution.</li>
          <li>Run the evaluation workbench on the tasks the report wants to move, to replace an
            estimated quality with a measured one.</li>
          <li>Enable the optimizer for one application, with a quality gate and a budget.</li>
          <li>Compare the savings waterfall against this projection.</li>
        </ol>
        <div class="note" style="margin-top:12px">Every step is reversible with one policy change,
          and step four is the first one that touches a production request.</div>`)}
    </div>

    ${(s.samples || []).length ? `<div class="section">
      <div class="head"><div><h2>Per request</h2>
        <p>What each request cost, and what the optimizer would have done with it.</p></div></div>
      ${card('', table([
        { label: 'Request', cell: (r) => `<div>${esc(truncate(r.query, 62))}</div>
            <div class="note mono" style="font-size:10px">${esc(r.request_id)}</div>` },
        { label: 'Served by', cell: (r) => esc((r.incumbent_model || '').split('/').pop()) },
        { label: 'Cost', align: 'r', cell: (r) => `<span class="num">${money(r.incumbent_cost_usd)}</span>` },
        { label: 'Would have used', cell: (r) => r.would_cache
            ? chip(`${r.cache_kind} cache`, 'info')
            : esc((r.projected_model || '').split('/').pop()) },
        { label: 'Would have cost', align: 'r', cell: (r) => `<span class="num">${money(r.projected_cost_usd)}</span>` },
        { label: 'Saving', align: 'r', cell: (r) => `<b class="num" style="color:var(--save)">${pct(r.projected_saving_pct, 0)}</b>` },
        { label: 'Basis', cell: (r) => basisChip(r.basis) },
      ], s.samples, { onRow: true, minWidth: 880 }), { pad: false })}
    </div>` : ''}`;
}

async function run(btn) {
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Running…';
  try {
    await post('/api/demo/scenario/shadow');
    toast('Shadow traffic complete. Production models served every answer.');
    go('shadow');
  } catch (e) {
    toast(e.message);
    btn.disabled = false;
    btn.textContent = 'Run shadow traffic';
  }
}
