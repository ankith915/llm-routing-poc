// The request trace: one request, every decision, in the order it happened.
// Shared by the requests table, the live flow page and presentation mode.
import { get } from '../api.js';
import { basisChip, chip, drawer, esc, int, json, kv, money, ms, pct, table, tierChip, tokens, truncate } from '../ui.js';

const STAGE_LABEL = {
  policy: 'Policy', budget: 'Budget', exact_cache: 'Exact cache', classify: 'Task classifier',
  semantic_cache: 'Semantic cache', route: 'Model routing', context: 'Context optimizer',
  plan: 'Execution plan', execute: 'Execution', quality: 'Quality gate', escalate: 'Escalation',
  ledger: 'Cost ledger', shadow: 'Shadow projection',
};
const STAGE_GLYPH = {
  policy: '⚖', budget: '$', exact_cache: '=', classify: '◇', semantic_cache: '≈', route: '⇉',
  context: '◧', plan: '▤', execute: '▶', quality: '✓', escalate: '↑', ledger: '∑', shadow: '◑',
};

export async function openTrace(requestId) {
  const d = drawer(`<div class="dhead"><div class="kicker">Request trace</div>
    <h1 class="mono" style="font-size:18px">${esc(requestId)}</h1></div>
    <div class="dbody"><div class="empty"><span class="spin"></span></div></div>`);
  try {
    const r = await get(`/api/requests/${encodeURIComponent(requestId)}`);
    d.innerHTML = traceDrawer(r);
  } catch (e) {
    d.querySelector('.dbody').innerHTML =
      `<div class="callout risk">Could not load that request: ${esc(e.message)}</div>`;
  }
}
window.openTrace = openTrace;

export function traceDrawer(r) {
  return `
  <div class="dhead">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:16px">
      <div style="min-width:0">
        <div class="kicker">Request trace · ${esc(r.tenant_id || '')} / ${esc(r.application_id || '')}</div>
        <h1 style="font-size:17px;font-weight:600">${esc(truncate(r.query, 130))}</h1>
        <div class="btn-row" style="margin-top:9px">
          <span class="mono" style="font-size:11px;color:var(--ink-4)">${esc(r.request_id)}</span>
          ${r.task_label ? chip(r.task_label) : ''}
          ${r.difficulty ? chip(r.difficulty) : ''}
          ${r.cache_kind ? chip(`${r.cache_kind} cache`, 'info') : tierChip(r.tier)}
          ${r.escalated_from ? chip('escalated', 'warn') : ''}
          ${r.fallback_used ? chip('fallback', 'warn') : ''}
          ${r.rejected ? chip('rejected', 'risk') : ''}
        </div>
      </div>
      <button class="btn ghost sm" onclick="closeDrawer()">✕</button>
    </div>
  </div>
  <div class="dbody">
    ${r.rejected ? `<div class="callout risk" style="margin-bottom:14px"><b>This request was rejected
      before any model was called.</b> ${esc(r.rejection_reason || '')}</div>` : ''}
    ${verdict(r)}
    <div class="section" style="margin-top:22px">
      <h2>What happened, in order</h2>
      <p class="note" style="margin:2px 0 14px">Every stage records what it decided, what it cost and
        how long it took. This is the whole answer to "why did this request cost that much".</p>
      <div class="trace">${(r.trace || []).map(step).join('')}</div>
    </div>
    ${answerBlock(r)}
  </div>`;
}

function verdict(r) {
  const saved = (r.baseline_cost_usd || 0) - (r.cost_usd || 0);
  return `
  <div class="grid g4" style="gap:10px">
    <div class="stat"><div class="l">Actual cost</div><div class="v">${money(r.cost_usd)}</div>
      <div class="n">${int(r.input_tokens)} in / ${int(r.output_tokens)} out tokens</div></div>
    <div class="stat"><div class="l">Baseline</div><div class="v">${money(r.baseline_cost_usd)}</div>
      <div class="n">${basisChip(r.baseline_source || 'estimated')}</div></div>
    <div class="stat ${saved > 0 ? 'good' : ''}"><div class="l">Saved</div>
      <div class="v">${pct(r.savings_pct)}</div><div class="n">${money(saved)}</div></div>
    <div class="stat ${r.quality_gate_passed === false ? 'risk' : (r.quality_score >= 4 ? 'good' : '')}">
      <div class="l">Quality</div>
      <div class="v">${r.quality_score != null ? r.quality_score : '—'}<small>${r.quality_score != null ? ' / 5' : ''}</small></div>
      <div class="n">${r.quality_gate_passed === true ? 'gate passed'
        : r.quality_gate_passed === false ? 'gate FAILED' : 'not gated'} · ${ms(r.latency_ms)}</div></div>
  </div>
  ${(r.ledger?.attribution || []).length ? `
  <div class="card" style="margin-top:14px"><div class="pad-sm">
    <div class="kicker" style="margin-bottom:9px">Cost attribution for this request</div>
    ${r.ledger.attribution.map((a) => `
      <div style="display:flex;justify-content:space-between;gap:12px;padding:5px 0;
        border-bottom:1px solid var(--line-2);font-size:12px">
        <span style="min-width:0">${esc(a.label)}
          <span class="note" style="display:block">${esc(a.detail)}</span></span>
        <span style="white-space:nowrap;text-align:right">
          <b class="num" style="color:${a.usd >= 0 ? 'var(--save)' : 'var(--risk)'}">
            ${a.usd >= 0 ? '−' : '+'}${money(Math.abs(a.usd))}</b><br>${basisChip(a.basis)}</span>
      </div>`).join('')}
    <div style="display:flex;justify-content:space-between;padding-top:9px;font-size:12.5px">
      <b>Net</b><b class="num" style="color:var(--save)">${money(r.ledger.savings_usd)}</b></div>
  </div></div>` : ''}`;
}

function step(s) {
  const body = DETAIL[s.stage] ? DETAIL[s.stage](s.detail || {}, s) : '';
  return `<div class="tstep ${esc(s.status)}">
    <div class="rail"><div class="node">${STAGE_GLYPH[s.stage] || '·'}</div><div class="line"></div></div>
    <div class="body">
      <div class="hd">
        <b style="font-size:12.5px">${esc(STAGE_LABEL[s.stage] || s.stage)}</b>
        <span class="st">${esc(s.status)}</span>
        ${s.cost_usd ? `<span class="chip">${money(s.cost_usd)}</span>` : ''}
        ${s.latency_ms ? `<span class="chip">${ms(s.latency_ms)}</span>` : ''}
        ${s.tokens ? `<span class="chip">${tokens(s.tokens)} tok</span>` : ''}
      </div>
      <div class="sm">${esc(s.summary)}</div>
      ${body}
      <details class="det"><summary>raw stage data</summary>${json(s.detail)}</details>
    </div>
  </div>`;
}

// --------------------------------------------------- per-stage detail views
const DETAIL = {
  policy: (d) => {
    const v = d.values || {};
    const src = (k) => `<span class="note">(${esc((d.provenance || {})[k] || 'defaults')})</span>`;
    return kv([
      ['quality target', `${v.quality_target} ${src('quality_target')}`],
      ['gate threshold', `${v.quality_gate_threshold} ${src('quality_gate_threshold')}`],
      ['latency target', `${ms(v.latency_target_ms)} ${src('latency_target_ms')}`],
      ['max cost', `${money(v.max_cost_per_request_usd)} ${src('max_cost_per_request_usd')}`],
      ['sensitivity', `${esc(v.sensitivity_class)} ${src('sensitivity_class')}`],
      ['SLA', `${esc(v.sla_class)} ${src('sla_class')}`],
      ['prompt / knowledge', `${esc(v.prompt_version)} / ${esc(v.knowledge_version)}`],
      [d.known_tenant === false ? 'tenant' : '', d.known_tenant === false
        ? '<span class="note warn">not in the policy file — defaults applied</span>' : ''],
    ]);
  },
  budget: (d) => (d.budgets || []).filter((b) => b.limit_usd).length
    ? table([
        { label: 'Scope', cell: (b) => `<span class="mono" style="font-size:10.5px">${esc(b.scope)}</span>` },
        { label: 'Window', cell: (b) => esc(b.window) },
        { label: 'Limit', align: 'r', cell: (b) => money(b.limit_usd) },
        { label: 'Used', align: 'r', cell: (b) => money(b.spent_usd + b.reserved_usd) },
        { label: 'Pressure', align: 'r', cell: (b) => pct(b.pressure * 100, 0) },
      ], (d.budgets || []).filter((b) => b.limit_usd), { minWidth: 380 })
    : `<div class="note">No budget limit on this scope. Worst-case reservation was
       ${money(d.estimated_worst_case_usd)} (premium model, full context) so concurrent traffic
       cannot overshoot a limit that does exist.</div>`,
  exact_cache: (d) => d.hit
    ? `<ul class="guards">${(d.hit.guards || []).map((g) => `<li>${esc(g)}</li>`).join('')}</ul>
       <div class="note" style="margin-top:6px">Avoided ${money(d.hit.avoided_cost_usd)} and
       ${ms(d.hit.avoided_latency_ms)} by reusing ${esc(d.hit.source_request_id)}.</div>`
    : (d.rejected || []).length
      ? `<div class="note">Closest entry rejected: ${esc(d.rejected[0].reason)}</div>` : '',
  classify: (d) => kv([
    ['task', `${esc(d.task_label)} <span class="note">${esc(d.task_type)}</span>`],
    ['difficulty', esc(d.difficulty)],
    ['decided by', `${esc(d.rung)} <span class="note">${d.rung === 'rules' ? 'free pattern match'
      : d.rung === 'classifier' ? 'free local classifier'
      : d.rung === 'llm_router' ? 'paid LLM call' : 'supplied by the caller'}</span>`],
    ['confidence', `${d.confidence} <span class="note">threshold ${d.threshold ?? '—'}</span>`],
    ['runners-up', Object.entries(d.alternatives || {}).slice(0, 3)
      .map(([k, v]) => `${esc(k)} ${v}`).join(', ')],
    ['note', d.note ? `<span class="note">${esc(d.note)}</span>` : ''],
  ]),
  semantic_cache: (d) => `
    ${d.hit ? `<ul class="guards">${(d.hit.guards || []).map((g) => `<li>${esc(g)}</li>`).join('')}</ul>`
      : `<div class="note">Threshold ${d.threshold} — ${esc(d.threshold_basis || '')}</div>`}
    ${(d.rejected || []).length ? `
      <details class="det" open><summary>${d.rejected.length} candidate(s) rejected</summary>
        ${table([
          { label: 'Similarity', align: 'r', cell: (x) => `<span class="num">${x.similarity}</span>` },
          { label: 'Candidate', cell: (x) => esc(truncate(x.query || '', 60)) },
          { label: 'Why not', cell: (x) => `<span class="note">${esc(x.reason)}</span>` },
        ], d.rejected.slice(0, 6), { minWidth: 420 })}
      </details>` : ''}
    ${d.semantic_embedder === false ? `<div class="note warn" style="margin-top:6px">
      Lexical embedder in use: similarity is bag-of-words overlap, not meaning. The subject guards
      are what make a hit safe here.</div>` : ''}`,
  route: (d) => `
    <ul class="reasons">${(d.factors || []).map((f) => `<li>${esc(f)}</li>`).join('')}</ul>
    <details class="det" open><summary>${(d.candidates || []).length} candidates considered</summary>
      ${table([
        { label: '', cell: (c) => c.model === d.selected_model
          ? '<span class="chip save">chosen</span>' : (c.eligible ? '' : '<span class="chip">out</span>') },
        { label: 'Model', cell: (c) => `<b>${esc(c.label)}</b>` },
        { label: 'Est. cost', align: 'r', cell: (c) => `<span class="num">${money(c.expected_cost_usd)}</span>` },
        { label: 'Est. quality', align: 'r', cell: (c) =>
          `<span class="num" style="color:${c.meets_quality ? 'var(--save)' : 'var(--risk)'}">${c.expected_quality}</span>` },
        { label: 'Est. latency', align: 'r', cell: (c) => `<span class="num">${ms(c.expected_latency_ms)}</span>` },
        { label: 'Basis', cell: (c) => `<span class="note">${esc(c.quality_source)}</span>` },
        { label: 'Verdict', cell: (c) => c.eligible
          ? (c.meets_quality ? '<span class="note">eligible</span>'
             : '<span class="note warn">below quality target</span>')
          : `<span class="note risk">${esc((c.exclusions || [])[0] || '')}</span>` },
      ], d.candidates || [], { minWidth: 720 })}
    </details>`,
  context: (d) => `
    ${kv([
      ['sections kept', (d.sections_kept || []).join(', ') || '—'],
      ['sections dropped', (d.sections_dropped || []).length
        ? `<span class="note">${esc((d.sections_dropped || []).join(', '))}</span>` : '—'],
      ['cacheable prefix', `${int(d.static_tokens)} tokens static / ${int(d.dynamic_tokens)} dynamic
        <span class="note">system prompt and context first, question last, so a provider prefix cache can match</span>`],
    ])}
    ${(d.transforms || []).length ? table([
      { label: 'Transform', cell: (t) => esc(t.name) },
      { label: 'Before', align: 'r', cell: (t) => `<span class="num">${int(t.tokens_before)}</span>` },
      { label: 'After', align: 'r', cell: (t) => `<span class="num">${int(t.tokens_after)}</span>` },
      { label: 'Saved', align: 'r', cell: (t) => t.tokens_saved
        ? `<b class="num" style="color:var(--save)">${int(t.tokens_saved)}</b>` : '—' },
      { label: 'Detail', cell: (t) => `<span class="note">${esc(t.detail)}</span>` },
    ], d.transforms, { minWidth: 560 }) : ''}`,
  plan: (d) => `<ul class="reasons">${(d.reasons || []).map((x) => `<li>${esc(x)}</li>`).join('')}</ul>`,
  execute: (d) => `
    ${kv([
      ['model', `${esc(d.model)} <span class="note">on ${esc(d.provider)}</span>`],
      ['tokens', `${int(d.input_tokens)} in / ${int(d.output_tokens)} out`
        + (d.cached_input_tokens ? ` · ${int(d.cached_input_tokens)} served from the provider prefix cache` : '')
        + (d.reasoning_tokens ? ` · ${int(d.reasoning_tokens)} hidden reasoning` : '')],
      ['finish reason', esc(d.finish_reason || '')],
    ])}
    ${(d.attempts || []).length > 1 ? table([
      { label: 'Attempt', cell: (a) => esc(a.model) },
      { label: 'Result', cell: (a) => a.ok ? '<span class="chip save">answered</span>'
        : `<span class="chip risk">${esc(a.reason || 'failed')}</span>` },
      { label: 'Detail', cell: (a) => `<span class="note">${esc(a.error || '')}${a.injected ? ' (injected fault)' : ''}</span>` },
    ], d.attempts, { minWidth: 420 }) : ''}`,
  quality: (d) => `
    ${(d.validators || []).length ? table([
      { label: 'Check', cell: (v) => esc(v.kind) },
      { label: 'Result', cell: (v) => v.passed ? '<span class="chip save">pass</span>'
        : '<span class="chip risk">fail</span>' },
      { label: 'Score', align: 'r', cell: (v) => `<span class="num">${v.score}</span>` },
      { label: 'Detail', cell: (v) => `<span class="note">${esc(v.detail)}</span>` },
    ], d.validators, { minWidth: 460 }) : ''}
    ${d.judge ? `<div class="note" style="margin-top:8px">
      LLM judge (${esc(d.judge.judge_model || '')}) scored <b>${d.judge.quality_score}</b>:
      ${esc(d.judge.quality_rationale || '')}. Judge calls are offline evaluation and are excluded
      from the request's cost.</div>` : ''}`,
  escalate: (d) => `<div class="note">Triggered by: ${esc(d.trigger || '')}</div>`,
  ledger: () => '',
  shadow: (d) => kv([
    ['served by', `${esc(d.incumbent_model)} at ${money(d.incumbent_cost_usd)}`],
    ['optimizer would have used', `${esc(d.projected_model || 'cache')} at ${money(d.projected_cost_usd)}`],
    ['projected saving', `<b style="color:var(--save)">${pct(d.projected_saving_pct)}</b> ${basisChip('estimated')}`],
  ]),
};

export { STAGE_LABEL, STAGE_GLYPH };


function answerBlock(r) {
  if (!r.answer) return '';
  return `<div class="section" style="margin-top:22px">
    <h2>Answer</h2>
    <div class="card" style="margin-top:8px"><div class="pad">
      <div style="white-space:pre-wrap;font-size:13px;line-height:1.65">${esc(r.answer)}</div>
    </div></div>
    ${r.quality_reason ? `<div class="note" style="margin-top:8px">Gate: ${esc(r.quality_reason)}</div>` : ''}
  </div>`;
}
