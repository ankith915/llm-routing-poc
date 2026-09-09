// Inline SVG charts. No library: every chart here is small, and a dependency
// that cannot be loaded offline is a liability in a client demo.
import { esc, money } from './ui.js';

const CSSVAR = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
export const tierColor = (t) => CSSVAR(`--tier-${['cheap', 'medium', 'premium', 'cache'].includes(t) ? t : 'cache'}`) || '#888';
export const palette = () => [CSSVAR('--tier-cheap'), CSSVAR('--tier-medium'), CSSVAR('--tier-premium'),
  CSSVAR('--tier-cache'), CSSVAR('--warn'), CSSVAR('--risk')];

const SHORT_LAYER = {
  'Exact cache': 'Exact\ncache', 'Semantic cache': 'Semantic\ncache',
  'Context optimisation': 'Context', 'Model routing': 'Routing',
  'Provider prompt cache': 'Provider\ncache', 'Reasoning optimisation': 'Reasoning',
  'Execution mode': 'Execution\nmode', 'Optimizer overhead': 'Overhead',
  'Quality escalations': 'Escalations', 'Provider fallbacks': 'Fallbacks',
};

/** Savings waterfall: baseline on the left, one bar per layer, final on the right.
 *  Green bars are money kept, red bars money the optimizer spent to keep it. */
export function waterfall(steps, baseline, final, { width = 900, height = 330 } = {}) {
  if (!baseline) return '<div class="empty">No spend recorded yet.</div>';
  const pad = { l: 10, r: 10, t: 30, b: 62 };
  const cols = [
    { label: 'Baseline', short: 'Baseline', usd: baseline, type: 'total', lo: 0, hi: baseline },
    ...steps.map((s) => ({
      label: s.label, short: SHORT_LAYER[s.label] || s.label, usd: s.usd, type: s.usd >= 0 ? 'save' : 'cost',
      lo: Math.min(s.from_usd, s.to_usd), hi: Math.max(s.from_usd, s.to_usd), basis: s.basis })),
    { label: 'Actual', short: 'Actual', usd: final, type: 'total', lo: 0, hi: final },
  ];
  const maxV = Math.max(baseline, final, ...cols.map((c) => c.hi)) * 1.06 || 1;
  const bw = (width - pad.l - pad.r) / cols.length;
  const inner = Math.min(bw * 0.6, 62);
  const y = (v) => pad.t + (1 - v / maxV) * (height - pad.t - pad.b);
  const save = CSSVAR('--save'), risk = CSSVAR('--risk');
  const total = CSSVAR('--ink-3'), line = CSSVAR('--line');
  let g = '';
  cols.forEach((c, i) => {
    const cx = pad.l + i * bw + bw / 2;
    const x = cx - inner / 2;
    const top = y(c.hi), bot = y(c.lo);
    const h = Math.max(2.5, bot - top);
    const fill = c.type === 'total' ? total : (c.type === 'save' ? save : risk);
    g += `<rect x="${x}" y="${top}" width="${inner}" height="${h}" rx="2" fill="${fill}"
            fill-opacity="${c.type === 'total' ? 0.9 : 1}">
            <title>${esc(c.label)}: ${money(Math.abs(c.usd))}</title></rect>`;
    if (i < cols.length - 1) {
      const yLink = y(c.type === 'total' ? c.hi : c.lo);
      g += `<line x1="${x + inner}" x2="${pad.l + (i + 1) * bw + bw / 2 - inner / 2}"
              y1="${yLink}" y2="${yLink}" stroke="${line}" stroke-dasharray="2 3"/>`;
    }
    // Totals show their value; steps show the signed delta, once.
    const label = c.type === 'total' ? money(c.usd)
      : `${c.usd >= 0 ? '\u2212' : '+'}${money(Math.abs(c.usd))}`;
    g += `<text x="${cx}" y="${top - 8}" text-anchor="middle" font-size="10.5" font-weight="600"
            fill="${c.type === 'total' ? CSSVAR('--ink') : fill}"
            font-family="var(--mono)">${esc(label)}</text>`;
    c.short.split('\n').forEach((ln, li) => {
      g += `<text x="${cx}" y="${height - pad.b + 18 + li * 11}" text-anchor="middle"
              font-size="9.5" class="tick">${esc(ln)}</text>`;
    });
    if (c.basis?.length) {
      const li = c.short.split('\n').length;
      g += `<text x="${cx}" y="${height - pad.b + 18 + li * 11}" text-anchor="middle"
              font-size="8" class="tick" opacity=".7">${esc(c.basis[0])}</text>`;
    }
  });
  return `<svg class="chart" viewBox="0 0 ${width} ${height}" role="img"
    aria-label="Savings waterfall from ${money(baseline)} baseline to ${money(final)} actual">${g}</svg>`;
}

/** Cost-versus-quality scatter. The economically dominant points sit lower-right. */
export function scatter(points, { width = 560, height = 300, xLabel = 'Cost per request (USD)', yLabel = 'Quality (1–5)' } = {}) {
  const pts = points.filter((p) => p.x != null && p.y != null);
  if (!pts.length) return '<div class="empty">Nothing graded yet.</div>';
  const pad = { l: 44, r: 18, t: 18, b: 42 };
  const xMax = Math.max(...pts.map((p) => p.x)) * 1.18 || 1;
  const yMin = Math.min(3, ...pts.map((p) => p.y)) - 0.2;
  const yMax = 5.05;
  const sx = (v) => pad.l + (v / xMax) * (width - pad.l - pad.r);
  const sy = (v) => pad.t + (1 - (v - yMin) / (yMax - yMin)) * (height - pad.t - pad.b);
  const line = CSSVAR('--line-2'), ink = CSSVAR('--ink'), ink3 = CSSVAR('--ink-3');
  let g = '';
  for (let v = Math.ceil(yMin * 2) / 2; v <= 5; v += 0.5) {
    g += `<line x1="${pad.l}" x2="${width - pad.r}" y1="${sy(v)}" y2="${sy(v)}" class="axis-line"/>
          <text x="${pad.l - 8}" y="${sy(v) + 3.5}" text-anchor="end" class="tick">${v.toFixed(1)}</text>`;
  }
  for (let i = 0; i <= 3; i++) {
    const v = (xMax / 3) * i;
    g += `<text x="${sx(v)}" y="${height - pad.b + 15}" text-anchor="middle" class="tick">${money(v)}</text>`;
  }
  pts.forEach((p) => {
    const c = p.color || tierColor(p.tier);
    const r = 5 + Math.min(9, Math.sqrt(p.size || 1) * 1.6);
    g += `<circle cx="${sx(p.x)}" cy="${sy(p.y)}" r="${r + 5}" fill="${c}" fill-opacity=".14"/>
          <circle cx="${sx(p.x)}" cy="${sy(p.y)}" r="${r}" fill="${c}">
            <title>${esc(p.label)}: ${money(p.x)}, quality ${p.y.toFixed(2)}${p.size ? `, ${p.size} requests` : ''}</title>
          </circle>
          <text x="${sx(p.x)}" y="${sy(p.y) - r - 6}" text-anchor="middle" font-size="10"
            font-weight="600" fill="${ink}" font-family="var(--sans)">${esc(p.label)}</text>`;
  });
  g += `<text x="${(width + pad.l) / 2}" y="${height - 4}" text-anchor="middle" class="tick"
          font-size="9" letter-spacing=".06em">${esc(xLabel.toUpperCase())}</text>
        <text x="11" y="${(height - pad.b) / 2}" text-anchor="middle" class="tick" font-size="9"
          letter-spacing=".06em" transform="rotate(-90 11 ${(height - pad.b) / 2})">${esc(yLabel.toUpperCase())}</text>`;
  void ink3;
  return `<svg class="chart" viewBox="0 0 ${width} ${height}" role="img"
    aria-label="${esc(xLabel)} against ${esc(yLabel)}">${g}</svg>`;
}

/** Horizontal ranked bars: the workhorse for "where does the money go". */
export function ranked(rows, { valueFmt = money, width = 100, colorFn = null, showTrack = true } = {}) {
  if (!rows.length) return '<div class="empty">Nothing to show.</div>';
  const max = Math.max(...rows.map((r) => Math.abs(r.value))) || 1;
  return `<div style="display:flex;flex-direction:column;gap:10px">${rows.map((r) => `
    <div>
      <div style="display:flex;justify-content:space-between;gap:12px;font-size:12px;align-items:baseline">
        <span style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.labelHtml || esc(r.label)}</span>
        <b class="num" style="white-space:nowrap">${valueFmt(r.value)}</b>
      </div>
      ${showTrack ? `<div class="bar" style="margin-top:4px">
        <div style="width:${Math.max(1.5, (100 * Math.abs(r.value)) / max) * (width / 100)}%;
          background:${r.color || (colorFn ? colorFn(r) : CSSVAR('--accent'))}"></div></div>` : ''}
      ${r.sub ? `<div class="note" style="margin-top:3px">${r.sub}</div>` : ''}
    </div>`).join('')}</div>`;
}

/** A small sparkline for cumulative spend over the request sequence. */
export function sparkline(values, { width = 320, height = 52, color = null } = {}) {
  if (values.length < 2) return '';
  const max = Math.max(...values) || 1;
  const c = color || CSSVAR('--accent');
  const pts = values.map((v, i) =>
    `${(i / (values.length - 1)) * width},${height - (v / max) * (height - 6) - 3}`).join(' ');
  return `<svg class="chart" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" style="height:${height}px">
    <polyline points="${pts}" fill="none" stroke="${c}" stroke-width="1.75" stroke-linejoin="round"/>
    <polyline points="0,${height} ${pts} ${width},${height}" fill="${c}" fill-opacity=".1" stroke="none"/>
  </svg>`;
}

/** Two cumulative-cost lines: what it cost, against what it would have cost. */
export function dualLine(seriesA, seriesB, { width = 720, height = 220, labelA = 'Baseline', labelB = 'Optimized' } = {}) {
  const n = Math.max(seriesA.length, seriesB.length);
  if (n < 2) return '<div class="empty">Run more than one request to see the two lines diverge.</div>';
  const pad = { l: 52, r: 14, t: 14, b: 28 };
  const max = Math.max(...seriesA, ...seriesB) || 1;
  const sx = (i) => pad.l + (i / (n - 1)) * (width - pad.l - pad.r);
  const sy = (v) => pad.t + (1 - v / max) * (height - pad.t - pad.b);
  const spend = CSSVAR('--spend'), save = CSSVAR('--save');
  const path = (s) => s.map((v, i) => `${i ? 'L' : 'M'}${sx(i)},${sy(v)}`).join(' ');
  let g = '';
  for (let i = 0; i <= 3; i++) {
    const v = (max / 3) * i;
    g += `<line x1="${pad.l}" x2="${width - pad.r}" y1="${sy(v)}" y2="${sy(v)}" class="axis-line"/>
          <text x="${pad.l - 8}" y="${sy(v) + 3.5}" text-anchor="end" class="tick">${money(v)}</text>`;
  }
  g += `<path d="${path(seriesA)} L${sx(seriesA.length - 1)},${sy(0)} L${sx(0)},${sy(0)} Z"
          fill="${spend}" fill-opacity=".08"/>
        <path d="${path(seriesA)}" fill="none" stroke="${spend}" stroke-width="2"/>
        <path d="${path(seriesB)}" fill="none" stroke="${save}" stroke-width="2"/>
        <text x="${sx(seriesA.length - 1)}" y="${sy(seriesA[seriesA.length - 1]) - 7}" text-anchor="end"
          font-size="10.5" font-weight="600" fill="${spend}" font-family="var(--sans)">${esc(labelA)} ${money(seriesA[seriesA.length - 1])}</text>
        <text x="${sx(seriesB.length - 1)}" y="${sy(seriesB[seriesB.length - 1]) + 15}" text-anchor="end"
          font-size="10.5" font-weight="600" fill="${save}" font-family="var(--sans)">${esc(labelB)} ${money(seriesB[seriesB.length - 1])}</text>
        <text x="${(width + pad.l) / 2}" y="${height - 3}" text-anchor="middle" class="tick" font-size="9">REQUESTS</text>`;
  return `<svg class="chart" viewBox="0 0 ${width} ${height}" role="img"
    aria-label="Cumulative ${esc(labelA)} against ${esc(labelB)} spend">${g}</svg>`;
}

/** Donut for a small categorical mix. */
export function donut(items, { size = 132, thickness = 20 } = {}) {
  const total = items.reduce((a, i) => a + i.value, 0);
  if (!total) return '';
  const r = (size - thickness) / 2;
  const c = 2 * Math.PI * r;
  let off = 0;
  const rings = items.map((i) => {
    const frac = i.value / total;
    const el = `<circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="${i.color}"
      stroke-width="${thickness}" stroke-dasharray="${frac * c} ${c}"
      stroke-dashoffset="${-off * c}" transform="rotate(-90 ${size / 2} ${size / 2})">
      <title>${esc(i.label)}: ${(frac * 100).toFixed(0)}%</title></circle>`;
    off += frac;
    return el;
  }).join('');
  return `<svg class="chart" viewBox="0 0 ${size} ${size}" style="width:${size}px;height:${size}px">${rings}</svg>`;
}
