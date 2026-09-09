// Thin fetch layer. One place for error shaping and the in-flight cache.
const inflight = new Map();

export async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { const d = await res.json(); detail = d.detail || d.message || detail; } catch { }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  const ct = res.headers.get('content-type') || '';
  return ct.includes('json') ? res.json() : res.text();
}

export const get = (p) => api(p);
export const post = (p, body) => api(p, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body ?? {}),
});
export const put = (p, body) => api(p, {
  method: 'PUT', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body ?? {}),
});

/** Fetch several endpoints at once; a failing one yields null rather than
 *  taking the whole page down. */
export async function all(map) {
  const keys = Object.keys(map);
  const vals = await Promise.all(keys.map((k) =>
    (typeof map[k] === 'string' ? get(map[k]) : map[k]).catch((e) => ({ __error: e.message }))));
  return Object.fromEntries(keys.map((k, i) => [k, vals[i]]));
}

/** De-duplicate identical concurrent GETs (the shell and a page may both want config). */
export function once(path) {
  if (!inflight.has(path)) {
    inflight.set(path, get(path).finally(() => setTimeout(() => inflight.delete(path), 1500)));
  }
  return inflight.get(path);
}
