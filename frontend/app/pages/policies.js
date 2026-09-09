import { all, put, post } from '../api.js';
import { card, chip, esc, int, json, money, ms, pct, stat, table, toast } from '../ui.js';
import { go } from '../main.js';

export async function render(root, state) {
  const d = await all({ pol: '/api/policies', budgets: '/api/budgets', flags: '/api/flags',
                        health: '/api/health/providers' });
  const pol = d.pol;
  if (pol?.__error) { root.innerHTML = `<div class="callout risk">${esc(pol.__error)}</div>`; return; }
  const selected = state.params.app
    || (pol.tenants[0]?.applications[0] ? `${pol.tenants[0].id}/${pol.tenants[0].applications[0].id}` : '');
  const [tid, aid] = selected.split('/');
  const tenant = pol.tenants.find((t) => t.id === tid) || pol.tenants[0];
  const app = tenant?.applications.find((a) => a.id === aid) || tenant?.applications[0];

  root.innerHTML = `
  <div class="page-head">
    <div><div class="kicker">Policy engine · ${esc(pol.policy_version)}</div>
      <h1>Who may spend what, on which models</h1>
      <p class="lede">Policy resolves defaults → tenant → application → request. Every value shows
        the layer that set it, so nobody has to guess why a request behaved the way it did.</p></div>
  </div>

  <div class="section">
    ${card('Feature flags', `
      <p class="note" style="margin:0 0 12px">Each flag turns one optimizer capability off at
        runtime. Turning one off and re-running the workload is the fastest way to show what that
        layer was actually contributing.</p>
      <div class="grid g3" style="gap:9px">
        ${Object.entries(pol.flags || {}).map(([k, f]) => `
          <label style="display:flex;align-items:center;gap:9px;padding:8px 11px;border:1px solid var(--line);
            border-radius:var(--r1);background:var(--surface-2);cursor:pointer;font-size:11.5px">
            <input type="checkbox" data-flag="${esc(k)}" ${f.value ? 'checked' : ''}
              style="width:auto;margin:0">
            <span style="flex:1">${esc(k.replace('ENABLE_', '').replace(/_/g, ' ').toLowerCase())}</span>
            <span class="note mono" style="font-size:9px">${esc(f.source.split(' ')[0])}</span>
          </label>`).join('')}
      </div>`)}
  </div>

  <div class="section">
    <div class="head"><div><h2>Applications</h2>
      <p>Pick one to see its effective policy and where each value came from.</p></div></div>
    <div class="seg" style="margin-bottom:12px;flex-wrap:wrap">
      ${pol.tenants.flatMap((t) => t.applications.map((a) =>
        `<button class="${`${t.id}/${a.id}` === `${tenant?.id}/${app?.id}` ? 'on' : ''}"
          data-app="${esc(t.id)}/${esc(a.id)}">${esc(t.label)} · ${esc(a.label)}</button>`)).join('')}
    </div>
    ${app ? effective(tenant, app, d.budgets) : ''}
  </div>

  <div class="section">
    <div class="head"><div><h2>Provider health</h2>
      <p>A provider with an open circuit breaker is deprioritised in routing but still usable as a
        last-resort fallback: availability beats cost.</p></div>
      <div class="btn-row">
        <button class="btn sm" data-chaos="groq">Break Groq</button>
        <button class="btn sm" data-chaos="openai">Break OpenAI</button>
        <button class="btn ghost sm" data-chaos="">Clear faults</button>
      </div></div>
    ${card('', providerHealth(d.health), { pad: false })}
  </div>`;

  root.querySelectorAll('[data-app]').forEach((b) => {
    b.onclick = () => go('policies', { app: b.dataset.app });
  });
  root.querySelectorAll('[data-flag]').forEach((cb) => {
    cb.onchange = async () => {
      try {
        await put('/api/flags', { name: cb.dataset.flag, value: cb.checked });
        toast(`${cb.dataset.flag} ${cb.checked ? 'enabled' : 'disabled'} for new requests.`);
      } catch (e) { toast(e.message); cb.checked = !cb.checked; }
    };
  });
  root.querySelectorAll('[data-chaos]').forEach((b) => {
    b.onclick = async () => {
      const p = b.dataset.chaos;
      try {
        if (p) await post('/api/demo/chaos', { provider: p, mode: 'fail' });
        else {
          await post('/api/demo/chaos', { provider: 'groq', mode: null });
          await post('/api/demo/chaos', { provider: 'openai', mode: null });
        }
        toast(p ? `${p} will now fail. Send a request to watch the fallback.` : 'Faults cleared.');
        go('policies', state.params);
      } catch (e) { toast(e.message); }
    };
  });
  const bBtn = root.querySelector('#save-budget');
  if (bBtn) bBtn.onclick = () => saveBudget(root, tenant.id, app.id);
}

function effective(tenant, app, budgets) {
  const v = app.policy || {};
  const prov = app.provenance || {};
  const src = (k) => `<span class="chip" style="font-size:9.5px">${esc(prov[k] || 'defaults')}</span>`;
  const scope = (budgets?.scopes || []).find((s) => s.application_id === app.id) || {};
  return `<div class="grid g-2-1">
    ${card(`${tenant.label} · ${app.label}`, `
      <div class="grid g2" style="gap:10px">
        ${[
          ['Quality target', v.quality_target, 'quality_target', 'minimum predicted quality a route must satisfy'],
          ['Gate threshold', v.quality_gate_threshold, 'quality_gate_threshold', 'observed score below which the answer is escalated'],
          ['Latency target', ms(v.latency_target_ms), 'latency_target_ms', ''],
          ['Max cost / request', money(v.max_cost_per_request_usd), 'max_cost_per_request_usd', ''],
          ['SLA class', v.sla_class, 'sla_class', 'decides the execution lane'],
          ['Sensitivity', v.sensitivity_class, 'sensitivity_class', 'restricts which providers may see the data'],
          ['Compression', `${v.compression?.mode} (min tier ${v.compression?.min_tier})`, 'compression.mode',
            'measured: folding context costs the cheap tier about 1.1 quality points and the frontier tier nothing'],
          ['Reasoning', v.reasoning_policy, 'reasoning_policy', ''],
          ['Frontier', `${v.frontier_allowed ? 'allowed' : 'blocked'}${v.frontier_preferred ? ', preferred' : ''}`,
            'frontier_allowed', ''],
          ['Unmeasured models', v.allow_unmeasured_models ? 'may be routed to' : 'excluded',
            'allow_unmeasured_models', 'when false, a model with no measured quality for that difficulty is not eligible'],
        ].map(([label, val, key, hint]) => `
          <div class="stat" style="gap:2px">
            <div class="l">${esc(label)} ${hint ? `<span title="${esc(hint)}" style="cursor:help">ⓘ</span>` : ''}</div>
            <div class="v" style="font-size:15px">${esc(String(val ?? '—'))}</div>
            <div class="n">${src(key)}</div>
          </div>`).join('')}
      </div>
      <details class="det" style="margin-top:14px"><summary>full effective policy</summary>
        ${json(v)}</details>`)}
    ${card('Budget', `
      ${(scope.budgets || []).map((b) => `
        <div style="margin-bottom:12px">
          <div style="display:flex;justify-content:space-between;font-size:12px">
            <span>${esc(b.window)}</span>
            <b class="num">${money(b.spent_usd)} / ${b.limit_usd ? money(b.limit_usd) : 'no limit'}</b></div>
          ${b.limit_usd ? `<div class="bar" style="margin-top:5px"><div style="width:${Math.min(100, b.pressure * 100)}%;
            background:${b.pressure > 0.8 ? 'var(--risk)' : b.pressure > 0.5 ? 'var(--warn)' : 'var(--save)'}"></div></div>
            <div class="note" style="margin-top:3px">pressure ${pct(b.pressure * 100, 0)}
              ${b.reserved_usd ? ` · ${money(b.reserved_usd)} reserved in flight` : ''}</div>` : ''}
        </div>`).join('')}
      <label class="field" style="margin-top:10px"><span class="lab">Set daily limit (USD)</span>
        <input type="number" id="daily" step="0.01" min="0"
          value="${(scope.budgets || []).find((b) => b.window === 'daily')?.limit_usd ?? ''}"></label>
      <div class="btn-row" style="margin-top:10px">
        <button class="btn primary sm" id="save-budget">Apply</button>
        <span class="note">Tighten it below current spend to watch routing adapt.</span>
      </div>`)}
  </div>`;
}

function providerHealth(h) {
  const rows = Object.entries(h?.providers || {});
  if (!rows.length) return '<div class="empty">No provider calls recorded yet.</div>';
  return table([
    { label: 'Provider', cell: ([k]) => `<b>${esc(k)}</b>` },
    { label: 'Breaker', cell: ([, p]) => p.breaker_open
        ? chip('OPEN', 'risk') : chip('closed', 'save') },
    { label: 'Error rate', align: 'r', cell: ([, p]) => `<span class="num">${pct(p.error_rate * 100, 0)}</span>` },
    { label: 'p50', align: 'r', cell: ([, p]) => `<span class="num">${p.p50_latency_ms ? ms(p.p50_latency_ms) : '—'}</span>` },
    { label: 'Samples', align: 'r', cell: ([, p]) => `<span class="num">${int(p.samples)}</span>` },
    { label: 'Injected fault', cell: ([, p]) => p.chaos ? chip(p.chaos, 'warn') : '—' },
    { label: 'Last error', cell: ([, p]) => `<span class="note">${esc((p.last_error || '').slice(0, 70))}</span>` },
  ], rows, { minWidth: 700 });
}

async function saveBudget(root, tenant, app) {
  const v = parseFloat(root.querySelector('#daily').value);
  try {
    await put('/api/budgets', { tenant_id: tenant, application_id: app,
      daily_usd: Number.isFinite(v) ? v : null });
    toast('Budget applied. The next request is checked against it before any model is called.');
    go('policies', { app: `${tenant}/${app}` });
  } catch (e) { toast(e.message); }
}
