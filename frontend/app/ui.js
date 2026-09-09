// Shared rendering vocabulary. Pages compose these; they do not hand-roll markup
// for things that appear more than once.

export const $ = (s, root = document) => root.querySelector(s);
export const $$ = (s, root = document) => [...root.querySelectorAll(s)];

export const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// ------------------------------------------------------------------ numbers
export function money(v, opts = {}) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const n = Number(v);
  if (opts.big || Math.abs(n) >= 1000) {
    return '$' + n.toLocaleString(undefined, { maximumFractionDigits: 0 });
  }
  if (Math.abs(n) >= 1) return '$' + n.toFixed(2);
  if (Math.abs(n) >= 0.01) return '$' + n.toFixed(4);
  if (n === 0) return '$0';
  return '$' + n.toFixed(Math.abs(n) < 0.000001 ? 8 : 6);
}
export const pct = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v))
  ? '—' : `${Number(v).toFixed(d)}%`;
export const int = (v) => (v === null || v === undefined) ? '—' : Number(v).toLocaleString();
export const ms = (v) => (v === null || v === undefined) ? '—'
  : (v < 1000 ? `${Math.round(v)}ms` : `${(v / 1000).toFixed(1)}s`);
export const tokens = (v) => (v === null || v === undefined) ? '—'
  : (v >= 1000 ? `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)}k` : String(v));

export const tierOf = (r) => r?.cache_kind ? 'cache' : (r?.tier || 'unknown');
export const tierClass = (t) => ['cheap', 'medium', 'premium', 'cache'].includes(t) ? t : 'unknown';

// ------------------------------------------------------------- components
export const stat = ({ label, value, sub, tone = '', hero = false, hint = '' }) => `
  <div class="stat ${hero ? 'hero' : ''} ${tone}">
    <div class="l">${esc(label)}${hint ? `<span title="${esc(hint)}" style="cursor:help">ⓘ</span>` : ''}</div>
    <div class="v">${value}</div>
    ${sub ? `<div class="n">${sub}</div>` : ''}
  </div>`;

export const card = (title, body, { sub = '', actions = '', pad = true } = {}) => `
  <div class="card">
    ${title ? `<div class="title"><h3>${esc(title)}</h3>
      <div style="display:flex;gap:10px;align-items:center">
        ${sub ? `<span class="sub">${esc(sub)}</span>` : ''}${actions}</div></div>` : ''}
    ${pad ? `<div class="pad">${body}</div>` : body}
  </div>`;

export const chip = (text, tone = '') => `<span class="chip ${tone}">${esc(text)}</span>`;

export const tierChip = (t) => {
  const cls = tierClass(t);
  const tone = { cheap: 'save', medium: 'warn', premium: 'spend', cache: 'info' }[cls] || '';
  return `<span class="chip ${tone}"><span class="dot bg-${cls}"></span>${esc(t || '—')}</span>`;
};

export const basisChip = (b) => {
  const tone = { measured: 'save', calibrated: 'save', estimated: 'warn', modeled: 'info' }[b] || '';
  const hint = {
    measured: 'Computed from real token counts returned by the provider.',
    calibrated: 'Estimated from the price registry, then corrected by the ratio observed on real frontier-model runs of comparable requests.',
    estimated: 'Derived from the price registry, not from a second real call.',
    modeled: 'A published rate applied to a scenario; not executed here.',
  }[b] || '';
  return `<span class="chip ${tone}" title="${esc(hint)}">${esc(b)}</span>`;
};

export const empty = (title, body = '', action = '') =>
  `<div class="empty"><div class="big">${esc(title)}</div>${body}${action ? `<div style="margin-top:16px">${action}</div>` : ''}</div>`;

export const loading = (label = 'Loading') =>
  `<div class="empty"><span class="spin"></span><div style="margin-top:10px">${esc(label)}…</div></div>`;

export const errorBox = (msg) =>
  `<div class="callout risk"><b>Could not load this view.</b> ${esc(msg)}</div>`;

export const bar = (segments) => `<div class="bar">${segments.map((s) =>
  `<div style="width:${s.pct}%;background:${s.color}" title="${esc(s.title || '')}"></div>`).join('')}</div>`;

export function stackBar(items, { height = 22 } = {}) {
  const total = items.reduce((a, i) => a + i.value, 0) || 1;
  return `<div class="stack" style="height:${height}px">${items.map((i) =>
    `<div style="width:${(100 * i.value / total).toFixed(2)}%;background:${i.color}"
      title="${esc(i.label)}: ${i.value}"></div>`).join('')}</div>
    <div class="legend">${items.map((i) =>
      `<span><span class="swatch" style="background:${i.color}"></span>${esc(i.label)}
        <b class="num" style="margin-left:6px">${((100 * i.value) / total).toFixed(0)}%</b></span>`).join('')}</div>`;
}

export const table = (cols, rows, { onRow = null, minWidth = 520 } = {}) => `
  <div class="tbl-wrap"><table style="min-width:${minWidth}px">
    <thead><tr>${cols.map((c) => `<th class="${c.align === 'r' ? 'r' : ''}">${esc(c.label)}</th>`).join('')}</tr></thead>
    <tbody>${rows.map((r, i) => `<tr class="${onRow ? 'click' : ''}" ${onRow ? `data-row="${i}"` : ''}>
      ${cols.map((c) => `<td class="${c.align === 'r' ? 'r' : ''}">${c.cell(r, i)}</td>`).join('')}</tr>`).join('')}
    </tbody></table></div>`;

export const field = (label, control, hint = '') => `
  <label class="field"><span class="lab">${esc(label)}${hint ? `<b>${esc(hint)}</b>` : ''}</span>${control}</label>`;

export const slider = (id, value, { min = 0, max = 100, step = 1 } = {}) =>
  `<input type="range" id="${id}" min="${min}" max="${max}" step="${step}" value="${value}">`;

// -------------------------------------------------------------- feedback
let toastTimer = null;
export function toast(msg, tone = '') {
  let el = $('#toast');
  if (!el) { el = document.createElement('div'); el.id = 'toast'; document.body.appendChild(el); }
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 4600);
}

// ---------------------------------------------------------------- drawer
export function drawer(html) {
  closeDrawer();
  const scrim = document.createElement('div');
  scrim.className = 'scrim';
  const d = document.createElement('div');
  d.className = 'drawer';
  d.innerHTML = html;
  document.body.append(scrim, d);
  requestAnimationFrame(() => { scrim.classList.add('show'); d.classList.add('show'); });
  scrim.onclick = closeDrawer;
  document.addEventListener('keydown', escClose);
  return d;
}
function escClose(e) { if (e.key === 'Escape') closeDrawer(); }
export function closeDrawer() {
  document.removeEventListener('keydown', escClose);
  $$('.scrim, .drawer').forEach((el) => {
    el.classList.remove('show');
    setTimeout(() => el.remove(), 240);
  });
}
window.closeDrawer = closeDrawer;

// --------------------------------------------------------------- helpers
export const ago = (iso) => {
  if (!iso) return '—';
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return `${Math.max(0, Math.round(s))}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
};
export const truncate = (s, n = 90) =>
  (s || '').length > n ? `${(s || '').slice(0, n)}…` : (s || '');

export const json = (o) => `<pre class="json">${esc(JSON.stringify(o, null, 2))}</pre>`;

export const kv = (pairs) => `<dl class="kv">${pairs.filter(([, v]) =>
  v !== null && v !== undefined && v !== '').map(([k, v]) =>
  `<dt>${esc(k)}</dt><dd>${v}</dd>`).join('')}</dl>`;

export const STRAT_SHORT = {
  none: 'Baseline', rule: 'Rules', intelligent: 'LLM router', optimized: 'Optimizer', shadow: 'Shadow',
};
