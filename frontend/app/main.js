// App shell: navigation, page lifecycle, presentation mode, global config.
import { once, get } from './api.js';
import { $, esc, toast, closeDrawer } from './ui.js';

export const state = {
  page: 'overview',
  config: null,
  params: {},
  timers: [],
  present: false,
};

const NAV = [
  { group: 'Money' },
  { id: 'overview', label: 'Overview', icon: '◧' },
  { id: 'waterfall', label: 'Savings waterfall', icon: '▤' },
  { id: 'waste', label: 'What is costing us', icon: '◔' },
  { id: 'recommendations', label: 'Recommendations', icon: '✦' },
  { group: 'Traffic' },
  { id: 'requests', label: 'Requests & traces', icon: '≡' },
  { id: 'flow', label: 'Live request flow', icon: '⇣' },
  { id: 'console', label: 'Query console', icon: '›' },
  { group: 'Decisions' },
  { id: 'models', label: 'Model economics', icon: '◇' },
  { id: 'policies', label: 'Policies & budgets', icon: '⚖' },
  { id: 'quality', label: 'Quality & gates', icon: '✓' },
  { group: 'Prove it' },
  { id: 'workbench', label: 'Evaluation workbench', icon: '⚗' },
  { id: 'shadow', label: 'Shadow mode', icon: '◑' },
  { id: 'simulator', label: 'What-if simulator', icon: '∿' },
  { group: 'Demo' },
  { id: 'present', label: 'Presentation mode', icon: '▷' },
];

const PAGES = {
  overview: () => import('./pages/overview.js'),
  waterfall: () => import('./pages/waterfall.js'),
  waste: () => import('./pages/waste.js'),
  recommendations: () => import('./pages/recommendations.js'),
  requests: () => import('./pages/requests.js'),
  flow: () => import('./pages/flow.js'),
  console: () => import('./pages/console.js'),
  models: () => import('./pages/models.js'),
  policies: () => import('./pages/policies.js'),
  quality: () => import('./pages/quality.js'),
  workbench: () => import('./pages/workbench.js'),
  shadow: () => import('./pages/shadow.js'),
  simulator: () => import('./pages/simulator.js'),
  present: () => import('./pages/present.js'),
};

export function clearTimers() {
  state.timers.forEach((t) => clearTimeout(t));
  state.timers = [];
}
export function later(fn, ms) {
  const t = setTimeout(fn, ms);
  state.timers.push(t);
  return t;
}

export function go(page, params = {}) {
  if (!PAGES[page]) page = 'overview';
  clearTimers();
  closeDrawer();
  state.page = page;
  state.params = params;
  const hash = `#${page}${Object.keys(params).length ? `?${new URLSearchParams(params)}` : ''}`;
  if (location.hash !== hash) history.replaceState(null, '', hash);
  document.body.classList.toggle('present', page === 'present');
  renderNav();
  render();
}
window.go = go;

function renderNav() {
  $('#nav').innerHTML = NAV.map((n) => n.group
    ? `<div class="group">${esc(n.group)}</div>`
    : `<button class="${state.page === n.id ? 'on' : ''}" data-go="${n.id}">
         <span class="ic">${n.icon}</span>${esc(n.label)}</button>`).join('');
}

async function render() {
  const main = $('#main');
  main.innerHTML = '<div class="card"><div class="empty"><span class="spin"></span></div></div>';
  try {
    const mod = await PAGES[state.page]();
    await mod.render(main, state);
  } catch (e) {
    console.error(e);
    main.innerHTML = `<div class="card"><div class="pad">
      <div class="callout risk"><b>This view failed to render.</b> ${esc(e.message)}</div>
      <div class="note" style="margin-top:10px">The details are in the browser console.
        Everything else in the app is unaffected.</div></div></div>`;
  }
}
export { render };

async function loadConfig() {
  try {
    state.config = await once('/api/config');
  } catch (e) {
    $('#side-foot').innerHTML = `<div style="color:var(--risk)">API unreachable</div>`;
    return;
  }
  const c = state.config;
  $('#brand-sub').textContent = c.principal?.tenant_id
    ? `${c.principal.tenant_id} · ${c.environment || 'production'}`
    : 'AI FinOps control plane';
  const flags = Object.values(c.flags || {}).filter((f) => f.value).length;
  const total = Object.keys(c.flags || {}).length;
  $('#side-foot').innerHTML = `
    <div class="row"><span>prices</span><b>${esc(c.price_registry_version)}</b></div>
    <div class="row"><span>policy</span><b>${esc(c.policy_version)}</b></div>
    <div class="row"><span>store</span><b>${esc(c.storage_backend)}</b></div>
    <div class="row"><span>features</span><b>${flags}/${total} on</b></div>
    ${c.api_key_configured ? '' : `<div style="color:var(--warn);margin-top:8px;line-height:1.5">
       No provider keys configured — requests will fail.</div>`}
    ${c.auth?.enabled ? '' : `<div style="color:var(--warn);margin-top:8px;line-height:1.5"
       title="${esc(c.auth?.note || '')}">Unsecured instance</div>`}
    <div style="margin-top:10px"><button class="btn ghost sm" id="theme-btn"
      style="padding:3px 7px;font-size:10px">theme</button></div>`;
  $('#theme-btn').onclick = toggleTheme;
}

function toggleTheme() {
  const root = document.documentElement;
  const cur = root.getAttribute('data-theme')
    || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  const next = cur === 'dark' ? 'light' : 'dark';
  root.setAttribute('data-theme', next);
  try { localStorage.setItem('theme', next); } catch { }
  render();
}

function initTheme() {
  try {
    const t = localStorage.getItem('theme');
    if (t) document.documentElement.setAttribute('data-theme', t);
  } catch { }
}

function routeFromHash() {
  const [page, qs] = (location.hash.slice(1) || 'overview').split('?');
  return [page, Object.fromEntries(new URLSearchParams(qs || ''))];
}

document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-go]');
  if (b) { e.preventDefault(); go(b.dataset.go, JSON.parse(b.dataset.params || '{}')); }
});
window.addEventListener('hashchange', () => {
  const [p, params] = routeFromHash();
  if (p !== state.page || JSON.stringify(params) !== JSON.stringify(state.params)) go(p, params);
});

initTheme();
loadConfig().then(() => {
  const [p, params] = routeFromHash();
  go(p, params);
});

export { toast, get };
