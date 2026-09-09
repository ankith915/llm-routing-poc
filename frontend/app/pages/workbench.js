// Run a dataset through several strategies and compare what came back.
import { all, get, post } from '../api.js';
import { card, chip, empty, esc, int, money, ms, pct, stat, table, toast, STRAT_SHORT } from '../ui.js';
import { scatter } from '../charts.js';
import { go, later } from '../main.js';

const STRATS = [
  ['none', 'Baseline', 'Every request to the frontier model'],
  ['rule', 'Rule router', 'Keyword heuristics pick a tier'],
  ['intelligent', 'LLM router', 'A small model classifies every request'],
  ['optimized', 'Full optimizer', 'Cache, classifier ladder, routing, context, gate'],
];

export async function render(root, state) {
  const d = await all({ status: '/api/experiment/status', m: '/api/metrics', cfg: '/api/config',
                        clf: '/api/classifier' });
  const st = d.status || {};
  const running = st.status === 'running';
  const strategies = (d.m?.comparison?.strategies || []).filter((s) => s.requests);

  root.innerHTML = `
  <div class="page-head">
    <div><div class="kicker">Evaluation workbench</div>
      <h1>Prove the optimizer works, on a dataset you can inspect</h1>
      <p class="lede">Every query runs through each selected strategy and is graded the same way.
        This is where an assumed quality prior becomes a measurement, and where a routing change is
        checked before it reaches a client.</p></div>
  </div>

  ${running ? progress(st) : setup(d)}

  ${strategies.length ? `
    <div class="section">
      <div class="head"><div><h2>Results</h2>
        <p>Identical query set, identical grading. Savings compare per-request averages so an uneven
          count cannot flatter a strategy.</p></div>
        <button class="btn sm" id="report">Plain-text report</button></div>
      ${card('', table([
        { label: 'Strategy', cell: (s) => `<b>${esc(s.label)}</b>` },
        { label: 'Requests', align: 'r', cell: (s) => `<span class="num">${int(s.requests)}</span>` },
        { label: 'Cost', align: 'r', cell: (s) => `<b class="num">${money(s.total_cost_usd)}</b>` },
        { label: 'Per request', align: 'r', cell: (s) => `<span class="num">${money(s.avg_cost_usd)}</span>` },
        { label: 'Per solved task', align: 'r', cell: (s) => `<span class="num">${money(s.cost_per_successful_task_usd)}</span>` },
        { label: 'Quality', align: 'r', cell: (s) => `<span class="num">${s.avg_quality ?? '—'}</span>` },
        { label: 'Retention', align: 'r', cell: (s) => `<span class="num">${pct(s.quality_retention_pct)}</span>` },
        { label: 'Cache', align: 'r', cell: (s) => `<span class="num">${pct(s.cache_hit_rate, 0)}</span>` },
        { label: 'Escalated', align: 'r', cell: (s) => `<span class="num">${pct(s.escalation_rate, 0)}</span>` },
        { label: 'Frontier', align: 'r', cell: (s) => `<span class="num">${pct(s.frontier_pct, 0)}</span>` },
        { label: 'P95', align: 'r', cell: (s) => `<span class="num">${ms(s.p95_latency_ms)}</span>` },
        { label: 'Saving', align: 'r', cell: (s) => s.strategy === 'none'
            ? '<span class="note">baseline</span>'
            : `<b class="num" style="color:var(--save)">${pct(s.cost_savings_pct)}</b>` },
      ], strategies, { minWidth: 1080 }), { pad: false })}
      <div class="note" style="margin-top:10px">
        <b>Quality retention is a ratio of means on an ordinal 1–5 scale.</b> It is a fair headline
        but weak under scrutiny, which is why the gate pass rate and the per-task table on the
        quality page sit beside it.
      </div>
    </div>

    <div class="grid g2 section">
      ${card('The cost–quality frontier', scatter(strategies.map((s) => ({
        x: s.avg_cost_usd, y: s.avg_quality, label: STRAT_SHORT[s.strategy] || s.strategy, size: s.requests,
        color: s.strategy === 'none' ? 'var(--spend)' : s.strategy === 'optimized' ? 'var(--save)' : 'var(--warn)',
      })), { width: 460, height: 280 }) + `<div class="note" style="margin-top:8px">
        A strategy that only moves left has bought savings with quality. One that moves left without
        moving down has advanced the frontier.</div>`)}
      ${card('Router accuracy', `
        <div class="grid g2" style="gap:10px">
          ${stat({ label: 'Task type', value: pct((d.clf?.accuracy?.task_type_accuracy || 0) * 100, 1) })}
          ${stat({ label: 'Difficulty', value: pct((d.clf?.accuracy?.difficulty_accuracy || 0) * 100, 1) })}
        </div>
        <div class="note" style="margin-top:12px">${esc(d.clf?.note || '')}</div>
        <div class="note" style="margin-top:8px">Trained on ${int(d.clf?.trained_on?.labelled)} labelled
          queries${d.clf?.trained_on?.logged ? ` plus ${int(d.clf.trained_on.logged)} decisions distilled
          from the paid LLM router` : ''}.</div>
        <button class="btn sm" id="retrain" style="margin-top:12px">Distil the LLM router into the free rung</button>`)}
    </div>` : ''}

  <div class="section">
    ${card('Compression × model tier — the finding that changed the design', `
      <p class="note" style="margin:0 0 12px;line-height:1.75;max-width:82ch">
        Context compression is sold as a free lever. Measured per model on this corpus it is not:
        a frontier model reassembles a folded timestamp block without noticing, and a 20B model
        misreads it and loses the incident timeline entirely. The damage tracks <b>which model reads
        the context</b>, not how hard the question is — and routing sends the most traffic to
        precisely the weakest model. The resolution is a policy, not a switch: compress for the
        medium and premium tiers, send the cheap tier the original. That is what
        <code>compression.mode: tier_aware</code> does, and it is why the context stage runs
        <em>after</em> routing rather than before it.
      </p>
      <div class="tbl-wrap"><table style="min-width:520px">
        <thead><tr><th>Model</th><th class="r">Quality, compression off</th>
          <th class="r">on</th><th class="r">Δ</th></tr></thead>
        <tbody>
          <tr><td>gpt-4.1 <span class="chip spend">premium</span></td><td class="r num">4.59</td>
            <td class="r num">4.61</td><td class="r num" style="color:var(--save)">+0.02</td></tr>
          <tr><td>gpt-oss-120b <span class="chip warn">medium</span></td><td class="r num">4.73</td>
            <td class="r num">4.50</td><td class="r num" style="color:var(--warn)">−0.23</td></tr>
          <tr><td>gpt-oss-20b <span class="chip save">cheap</span></td><td class="r num">4.64</td>
            <td class="r num">3.57</td><td class="r num" style="color:var(--risk)">−1.07</td></tr>
        </tbody></table></div>
      <div class="note" style="margin-top:10px">
        108 graded requests, 2026-09-08, every query × strategy run once uncompressed and once
        compressed. Context tokens fell 17.6% across the board.
        ${d.cfg?.compression_available
          ? 'Re-run it here with the compression A/B option above.'
          : 'The compressor is not installed on this host, so the A/B runs locally: <code>python run_experiment.py --compression both</code>.'}
      </div>`)}
  </div>`;

  const runBtn = root.querySelector('#run');
  if (runBtn) runBtn.onclick = () => start(root, state);
  const rep = root.querySelector('#report');
  if (rep) rep.onclick = async () => {
    try {
      const txt = await get('/api/experiment/report');
      const w = window.open('', '_blank');
      w.document.write(`<pre style="font:13px ui-monospace,monospace;padding:24px">${esc(txt)}</pre>`);
    } catch (e) { toast(e.message); }
  };
  const rt = root.querySelector('#retrain');
  if (rt) rt.onclick = async () => {
    rt.disabled = true; rt.innerHTML = '<span class="spin"></span> Retraining…';
    try {
      const r = await post('/api/classifier/retrain');
      toast(`Learned from ${r.learned_from_logged_decisions} logged router decision(s). ` +
            `Leave-one-out task accuracy now ${(r.accuracy.task_type_accuracy * 100).toFixed(1)}%.`);
      go('workbench');
    } catch (e) { toast(e.message); rt.disabled = false; rt.textContent = 'Distil the LLM router'; }
  };
  if (running) later(() => step(root, state), 700);
}

function setup(d) {
  const cfg = d.cfg || {};
  return card('Run an evaluation', `
    <div class="grid g3" style="gap:14px">
      <label class="field"><span class="lab">Query set</span>
        <select id="size">
          <option value="quick">Quick — ${int(cfg.demo_query_count)} stratified queries</option>
          <option value="full">Everything — ${int(cfg.total_query_count)} queries</option>
          <option value="9">Very quick — 9 queries</option>
        </select></label>
      <label class="field"><span class="lab">Compression</span>
        <select id="compression">
          <option value="off">Routing only</option>
          <option value="both" ${cfg.compression_available ? '' : 'disabled'}>
            Compression A/B ${cfg.compression_available ? '' : '(compressor not installed here)'}</option>
        </select></label>
      <div style="display:flex;align-items:flex-end">
        <button class="btn primary" id="run" style="width:100%">Run evaluation</button></div>
    </div>
    <div style="margin-top:14px">
      <div class="kicker" style="margin-bottom:8px">Strategies</div>
      <div class="grid g4" style="gap:9px">
        ${STRATS.map(([id, label, desc]) => `
          <label style="display:flex;gap:9px;align-items:flex-start;padding:10px 12px;
            border:1px solid var(--line);border-radius:var(--r1);background:var(--surface-2);cursor:pointer">
            <input type="checkbox" data-strat="${id}" checked style="width:auto;margin:3px 0 0">
            <span><b style="font-size:12px">${esc(label)}</b>
              <span class="note" style="display:block">${esc(desc)}</span></span>
          </label>`).join('')}
      </div>
    </div>
    <div class="note" style="margin-top:12px">
      A run clears the request log, the caches and the budget counters first, so each evaluation
      starts from the same state. Every answer is graded by the same validators and judge.
    </div>`);
}

function progress(st) {
  const pctDone = st.total ? Math.round((100 * st.completed) / st.total) : 0;
  return card('Evaluation running', `
    <div style="display:flex;justify-content:space-between;font-size:12.5px">
      <span class="mono">${int(st.completed)} / ${int(st.total)} requests</span>
      <span class="mono">${pctDone}%${eta(st)}</span></div>
    <div class="bar" style="margin-top:8px;height:10px"><div style="width:${pctDone}%;background:var(--accent)"></div></div>
    ${st.failed ? `<div class="note warn" style="margin-top:9px">${int(st.failed)} request(s) could not
      be completed and are excluded from the comparison.</div>` : ''}
    <div class="note" style="margin-top:9px">Work runs inside the poll request, so the run survives a
      serverless host freezing the function between calls.</div>`);
}

function eta(st) {
  if (!st.started_at || !st.completed) return '';
  const elapsed = Date.now() / 1000 - st.started_at;
  const left = Math.round((elapsed / st.completed) * (st.total - st.completed));
  if (!Number.isFinite(left) || left <= 0) return '';
  return ` · about ${left > 60 ? `${Math.floor(left / 60)}m ` : ''}${left % 60}s left`;
}

async function start(root, state) {
  const btn = root.querySelector('#run');
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Starting…';
  const size = root.querySelector('#size').value;
  const strategies = [...root.querySelectorAll('[data-strat]:checked')].map((c) => c.dataset.strat);
  if (!strategies.length) { toast('Pick at least one strategy.'); btn.disabled = false; return; }
  try {
    await post('/api/experiment/run', {
      full: size === 'full',
      limit: size === 'quick' ? null : (size === 'full' ? null : Number(size)),
      compression: root.querySelector('#compression').value,
      strategies,
    });
    go('workbench');
  } catch (e) {
    toast(e.message);
    btn.disabled = false;
    btn.textContent = 'Run evaluation';
  }
}

async function step(root, state) {
  try {
    const st = await post('/api/experiment/step');
    if (st.status === 'running') { go('workbench'); return; }
    toast('Evaluation complete.');
    go('workbench');
  } catch (e) {
    toast(`The run stopped: ${e.message}`);
  }
}
