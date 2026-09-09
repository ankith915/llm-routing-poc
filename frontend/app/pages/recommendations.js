import { get, put } from '../api.js';
import { card, chip, empty, esc, int, json, money, pct, stat, toast } from '../ui.js';
import { go } from '../main.js';

const CONF = { high: 'save', medium: 'warn', low: 'risk' };

export async function render(root, state) {
  const d = await get('/api/analytics/recommendations');
  const recs = d.recommendations || [];
  if (!recs.length) {
    root.innerHTML = head() + card('', empty('No recommendations yet',
      esc(d.note || 'Run more traffic so there is something to learn from.'),
      '<button class="btn primary" data-go="overview">Run the demo workload</button>'), { pad: false });
    return;
  }
  const total = recs.reduce((a, r) => a + r.saving_usd, 0);
  const safe = recs.filter((r) => r.apply_safe);
  root.innerHTML = head() + `
    <div class="grid g3">
      ${stat({ label: 'Recommendations', value: int(recs.length),
        sub: `${int(safe.length)} carry measured quality evidence` })}
      ${stat({ label: 'Projected saving', value: money(total), tone: 'good',
        sub: 'on the traffic recorded so far' })}
      ${stat({ label: 'Applied automatically', value: 'none',
        sub: 'every change needs a human approval',
        hint: 'The learning loop proposes; it does not push policy to production on its own.' })}
    </div>
    <div class="section"><div class="grid" style="gap:12px">
      ${recs.map(rec).join('')}
    </div></div>
    <div class="note" style="margin-top:16px">${esc(d.note)}</div>`;

  root.querySelectorAll('[data-apply]').forEach((b) => {
    b.onclick = () => apply(b, recs[Number(b.dataset.apply)]);
  });
}

function head() {
  return `<div class="page-head"><div>
    <div class="kicker">Recommendation engine</div>
    <h1>What to change, and what the evidence says</h1>
    <p class="lede">Each recommendation carries the policy patch it would apply and the measured
      quality evidence for or against it. A recommendation whose alternative model has never been
      graded on that task says so, and is not safe to apply.</p>
  </div><div class="btn-row"><button class="btn" data-go="workbench">Get the missing evidence</button></div></div>`;
}

function rec(r, i) {
  return `<div class="card"><div class="pad">
    <div style="display:flex;justify-content:space-between;gap:16px;align-items:flex-start">
      <div style="min-width:0">
        <div class="btn-row" style="margin-bottom:6px">
          ${chip(`${r.confidence} confidence`, CONF[r.confidence])}
          ${chip(`${int(r.sample)} observations`)}
          ${r.graded_alternative_sample ? chip(`${int(r.graded_alternative_sample)} graded on the alternative`, 'save')
            : chip('alternative not graded', 'warn')}
        </div>
        <h3 style="margin:0 0 4px;font-size:14px;font-weight:600">${esc(r.title)}</h3>
        <div class="note">${esc(r.action)}</div>
      </div>
      <div style="text-align:right;white-space:nowrap">
        <div class="mono" style="font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;
          color:var(--ink-4)">Saving</div>
        <div class="num" style="font-size:19px;font-weight:600;color:var(--save)">${money(r.saving_usd)}</div>
        <div class="note">${pct(r.saving_pct, 0)} of that traffic</div>
      </div>
    </div>
    <div class="callout ${r.apply_safe ? 'save' : 'warn'}" style="margin-top:12px">
      <b>Quality evidence.</b> ${esc(r.quality_evidence)}
    </div>
    <details class="det" style="margin-top:10px"><summary>the exact policy change</summary>
      ${json(r.policy_patch)}</details>
    <div class="btn-row" style="margin-top:12px">
      <button class="btn ${r.apply_safe ? 'primary' : ''} sm" data-apply="${i}"
        ${r.policy_patch?.application ? '' : 'disabled title="This one needs a config change, not a runtime override"'}>
        ${r.apply_safe ? 'Apply this policy' : 'Apply anyway'}</button>
      ${r.apply_safe ? '' : '<span class="note">The evidence does not support this yet.</span>'}
    </div>
  </div></div>`;
}

async function apply(btn, r) {
  const p = r.policy_patch || {};
  if (!p.application) { toast('This recommendation needs a config change rather than a runtime override.'); return; }
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Applying…';
  try {
    await put('/api/policies', { tenant_id: p.tenant || 'acme', application_id: p.application, patch: p.patch });
    toast(`Applied to ${p.application}. New traffic uses it immediately; existing records are untouched.`);
    go('policies');
  } catch (e) {
    toast(`Could not apply: ${e.message}`);
    btn.disabled = false;
    btn.textContent = 'Apply this policy';
  }
}
