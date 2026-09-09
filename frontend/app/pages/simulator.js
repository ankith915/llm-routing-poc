import { get, post } from '../api.js';
import { card, chip, esc, field, int, money, pct, slider, stat, table } from '../ui.js';
import { ranked } from '../charts.js';

let params = null;
let mode = 'whatif';

export async function render(root, state) {
  mode = state.params.mode || 'whatif';
  if (!params) {
    try { params = await get('/api/simulate/defaults'); } catch { params = {}; }
  }
  root.innerHTML = `
  <div class="page-head">
    <div><div class="kicker">Simulator · modelled, not measured</div>
      <h1>What would this cost at your volume?</h1>
      <p class="lede">Everything on this page is a model of spend under assumptions you control, not
        a measurement of your traffic. The defaults are seeded from the requests this system has
        actually recorded, so the starting point is real even though the projection is not.</p></div>
    <div class="seg">
      <button class="${mode === 'whatif' ? 'on' : ''}" data-go="simulator"
        data-params='{"mode":"whatif"}'>What-if</button>
      <button class="${mode === 'buildbuy' ? 'on' : ''}" data-go="simulator"
        data-params='{"mode":"buildbuy"}'>Build vs buy</button>
    </div>
  </div>
  <div id="body"></div>`;
  if (mode === 'whatif') await whatif(root);
  else await buildBuy(root);
}

// ------------------------------------------------------------------ what-if
async function whatif(root) {
  const body = root.querySelector('#body');
  const p = params;
  body.innerHTML = `<div class="grid g-1-2">
    <div>${card('Assumptions', `
      <div class="note" style="margin-bottom:14px">Seeded from ${esc(p.seeded_from || 'defaults')}.</div>
      ${field('Requests per month', `<input type="number" id="reqs" value="${p.requests_per_month}" step="10000">`)}
      <div class="grid g2" style="gap:10px;margin-top:10px">
        ${field('Input tokens', `<input type="number" id="inp" value="${p.input_tokens}" step="100">`)}
        ${field('Output tokens', `<input type="number" id="out" value="${p.output_tokens}" step="50">`)}
      </div>
      <div style="margin-top:14px">
        ${sliderRow('cache', 'Cache hit rate', p.cache_hit_rate)}
        ${sliderRow('ctx', 'Context reduction', p.context_reduction_pct)}
        ${sliderRow('batch', 'Batch-eligible traffic', p.batch_pct)}
      </div>
      <div class="kicker" style="margin:16px 0 8px">Model mix of served traffic</div>
      ${sliderRow('cheap', 'Cheap tier', p.mix.cheap)}
      ${sliderRow('medium', 'Balanced tier', p.mix.medium)}
      ${sliderRow('premium', 'Frontier tier', p.mix.premium)}
      <div class="note" style="margin-top:10px">The mix is normalised, so it never has to add to 100.</div>
    `)}</div>
    <div id="out-panel">${card('', '<div class="empty"><span class="spin"></span></div>', { pad: false })}</div>
  </div>`;

  const inputs = [...body.querySelectorAll('input')];
  let t;
  const run = async () => {
    const payload = {
      requests_per_month: num('#reqs'), input_tokens: num('#inp'), output_tokens: num('#out'),
      cache_hit_rate: num('#cache'), context_reduction_pct: num('#ctx'), batch_pct: num('#batch'),
      mix: { cheap: num('#cheap'), medium: num('#medium'), premium: num('#premium') },
      baseline_model: p.baseline_model,
    };
    body.querySelectorAll('[data-out]').forEach((el) => {
      el.textContent = `${document.querySelector(`#${el.dataset.out}`).value}%`;
    });
    try {
      const r = await post('/api/simulate', payload);
      body.querySelector('#out-panel').innerHTML = whatifResult(r);
    } catch (e) {
      body.querySelector('#out-panel').innerHTML = `<div class="callout risk">${esc(e.message)}</div>`;
    }
  };
  const num = (sel) => Number(body.querySelector(sel).value) || 0;
  inputs.forEach((i) => {
    i.oninput = () => { clearTimeout(t); t = setTimeout(run, 90); };
  });
  await run();
}

const sliderRow = (id, label, value) => `
  <label class="field" style="margin-bottom:10px">
    <span class="lab">${esc(label)}<b data-out="${id}">${value}%</b></span>
    ${slider(id, value, { min: 0, max: 100, step: 1 })}</label>`;

function whatifResult(r) {
  return card('Modelled monthly spend', `
    <div class="grid g3" style="gap:10px">
      ${stat({ label: 'Baseline', value: money(r.baseline.monthly_usd, { big: true }),
        sub: `${esc(r.baseline.model)} for everything` })}
      ${stat({ label: 'Optimized', value: money(r.optimized.monthly_usd, { big: true }), tone: 'good',
        sub: `${int(r.optimized.cached_requests)} cached, ${int(r.optimized.served_requests)} served` })}
      ${stat({ label: 'Saving', value: pct(r.savings.pct), hero: true,
        sub: `${money(r.savings.monthly_usd, { big: true })}/month · ${money(r.savings.annual_usd, { big: true })}/year` })}
    </div>
    <div class="section" style="margin-top:20px">
      <h2 style="font-size:13px">What each lever contributes</h2>
      <p class="note" style="margin:2px 0 12px">Computed by turning each lever off on its own, so
        overlapping effects are not double-counted.</p>
      ${ranked(r.lever_contributions.map((l) => ({ label: l.lever, value: l.usd })),
        { valueFmt: (v) => money(v, { big: true }) })}
    </div>
    <div class="section" style="margin-top:20px">
      <h2 style="font-size:13px">Per tier</h2>
      ${table([
        { label: 'Tier', cell: (l) => `${chip(l.tier)} <span class="note">${esc(l.model)}</span>` },
        { label: 'Share', align: 'r', cell: (l) => `<span class="num">${pct(l.share_pct, 0)}</span>` },
        { label: 'Requests', align: 'r', cell: (l) => `<span class="num">${int(l.requests)}</span>` },
        { label: 'Per request', align: 'r', cell: (l) => `<span class="num">${money(l.cost_per_request_usd)}</span>` },
        { label: 'Batch', align: 'r', cell: (l) => l.batch_discount
            ? `<span class="num" style="color:var(--save)">−${Math.round(l.batch_discount * 100)}%</span>`
            : '<span class="note">none</span>' },
        { label: 'Monthly', align: 'r', cell: (l) => `<b class="num">${money(l.monthly_usd, { big: true })}</b>` },
      ], r.optimized.lines, { minWidth: 560 })}
    </div>
    <div class="callout warn" style="margin-top:16px"><b>Read this as a sensitivity scenario, not a
      promise.</b> ${esc(r.note)}</div>`, { sub: `prices ${esc(r.price_registry_version)}` });
}

// --------------------------------------------------------------- build/buy
async function buildBuy(root) {
  const body = root.querySelector('#body');
  const defaults = { requests_per_day: 100000, input_tokens: 2000, output_tokens: 500,
    gpu_hourly_usd: 2.99, gpu_count: 2, utilization_pct: 40, tokens_per_second_per_gpu: 2500,
    engineering_monthly_usd: 12000, redundancy_factor: 1.5 };
  body.innerHTML = `<div class="grid g-1-2">
    <div>${card('Assumptions', `
      ${field('Requests per day', `<input type="number" id="rpd" value="${defaults.requests_per_day}" step="10000">`)}
      <div class="grid g2" style="gap:10px;margin-top:10px">
        ${field('Input tokens', `<input type="number" id="bi" value="${defaults.input_tokens}" step="100">`)}
        ${field('Output tokens', `<input type="number" id="bo" value="${defaults.output_tokens}" step="50">`)}
      </div>
      <div class="grid g2" style="gap:10px;margin-top:10px">
        ${field('GPU $/hour', `<input type="number" id="gh" value="${defaults.gpu_hourly_usd}" step="0.1">`)}
        ${field('GPU count', `<input type="number" id="gc" value="${defaults.gpu_count}" step="1" min="1">`)}
      </div>
      <div style="margin-top:12px">${sliderRow('util', 'Utilisation', defaults.utilization_pct)}</div>
      <div class="grid g2" style="gap:10px">
        ${field('Tokens/s per GPU', `<input type="number" id="tps" value="${defaults.tokens_per_second_per_gpu}" step="100">`)}
        ${field('Engineering $/month', `<input type="number" id="eng" value="${defaults.engineering_monthly_usd}" step="1000">`)}
      </div>
      <div class="note" style="margin-top:12px">Engineering and on-call are the line item that
        decides this, and the one most build/buy cases leave out.</div>`)}</div>
    <div id="bb-out">${card('', '<div class="empty"><span class="spin"></span></div>', { pad: false })}</div>
  </div>`;

  let t;
  const run = async () => {
    body.querySelectorAll('[data-out]').forEach((el) => {
      el.textContent = `${document.querySelector(`#${el.dataset.out}`).value}%`;
    });
    const n = (s) => Number(body.querySelector(s).value) || 0;
    try {
      const r = await post('/api/simulate/build-vs-buy', {
        requests_per_day: n('#rpd'), input_tokens: n('#bi'), output_tokens: n('#bo'),
        gpu_hourly_usd: n('#gh'), gpu_count: n('#gc'), utilization_pct: n('#util'),
        tokens_per_second_per_gpu: n('#tps'), engineering_monthly_usd: n('#eng'),
      });
      body.querySelector('#bb-out').innerHTML = bbResult(r);
    } catch (e) {
      body.querySelector('#bb-out').innerHTML = `<div class="callout risk">${esc(e.message)}</div>`;
    }
  };
  body.querySelectorAll('input').forEach((i) => {
    i.oninput = () => { clearTimeout(t); t = setTimeout(run, 90); };
  });
  await run();
}

function bbResult(r) {
  const selfCheaper = r.verdict === 'self_host';
  return card('API against self-hosting', `
    <div class="callout ${selfCheaper ? 'save' : 'warn'}" style="margin-bottom:16px">
      <b>${selfCheaper ? 'Self-hosting wins at this volume.' : 'Stay on the API.'}</b>
      ${esc(r.verdict_text)}
    </div>
    <div class="grid g2" style="gap:10px">
      ${stat({ label: 'API', value: money(r.api.monthly_usd, { big: true }),
        sub: `${esc(r.api.model)} · ${money(r.api.annual_usd, { big: true })}/year` })}
      ${stat({ label: 'Self-hosted, fully loaded', value: money(r.self_hosted.monthly_usd, { big: true }),
        tone: selfCheaper ? 'good' : 'warn',
        sub: `${pct(r.self_hosted.people_share_pct, 0)} of it is people, not GPUs` })}
    </div>
    <div class="section" style="margin-top:18px">
      <h2 style="font-size:13px">Where the self-hosted money goes</h2>
      ${ranked([
        { label: 'GPUs (with redundancy)', value: r.self_hosted.gpu_monthly_usd, color: 'var(--spend)' },
        { label: 'Engineering and on-call', value: r.self_hosted.people_monthly_usd, color: 'var(--risk)' },
        { label: 'Storage, egress, observability', value: r.self_hosted.other_monthly_usd, color: 'var(--warn)' },
      ], { valueFmt: (v) => money(v, { big: true }) })}
    </div>
    <div class="section" style="margin-top:18px">
      <h2 style="font-size:13px">Capacity and break-even</h2>
      ${table([
        { label: '', cell: (x) => `<b>${esc(x[0])}</b>` },
        { label: '', align: 'r', cell: (x) => `<span class="num">${x[1]}</span>` },
      ], [
        ['Workload', `${int(r.self_hosted.required_tokens_per_day)} tokens/day`],
        ['Fleet capacity at this utilisation', `${int(r.self_hosted.capacity_tokens_per_day)} tokens/day`],
        ['GPUs actually required', `${int(r.self_hosted.gpus_required)}`],
        ['Break-even volume', r.breakeven.requests_per_day
          ? `${int(r.breakeven.requests_per_day)} requests/day` : 'n/a'],
        ['Current volume', `${int(r.breakeven.current_tokens_per_day / (r.inputs.input_tokens + r.inputs.output_tokens))} requests/day`],
      ], { minWidth: 400 })}
    </div>
    <div class="note" style="margin-top:14px">${esc(r.note)}</div>`);
}
