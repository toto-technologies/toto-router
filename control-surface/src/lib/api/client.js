// Thin fetch wrapper for the admin API. Mirrors frontend/src/lib/api.ts:
//   - same-origin RELATIVE urls (the gateway serves this console in prod; vite proxies /v1 in dev)
//   - auth is the httpOnly `toto_session` cookie — it rides same-origin automatically, no token in JS
//   - errors come as {error:{message,code}} OR FastAPI-nested {detail:{error:...}}; unwrap both
// No $app/$env imports here on purpose so `node --test` can exercise it with a mocked global.fetch.

// Same-origin: empty base -> relative /v1/... . The prerender pass never runs these (pages guard
// with `browser`), so there's no non-browser base to fall back to like the SPA's session client.
const API_BASE = '';

/** A failed admin call. `status` (401/403/404/409/…) + `code` (invalid_token, insufficient_role,
 *  cross_org_denied, team_not_found, …) are what the result layer switches on for distinct states. */
export class ApiError extends Error {
  constructor(status, code, message) {
    super(message || `${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

/** Build a `?a=1&b=2` string, dropping null/undefined/'' (so optional params just vanish). */
function qs(params) {
  if (!params) return '';
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') u.set(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : '';
}

/**
 * One request. `body` present -> JSON-encoded with Content-Type (an unsafe method the gateway's
 * Origin==Host check guards; the dev proxy's changeOrigin:false keeps that passing). 204 -> null.
 * `headers` merges extra request headers (e.g. x-toto-escalated-from) over the JSON default.
 * @param {'GET'|'POST'|'PATCH'|'PUT'|'DELETE'} method
 * @param {string} path  e.g. '/v1/admin/teams'
 * @param {{query?: Record<string, any>, body?: any, headers?: Record<string, string>}} [opts]
 * @returns {Promise<any>}
 */
export async function api(method, path, { query, body, headers } = {}) {
  const res = await fetch(`${API_BASE}${path}${qs(query)}`, {
    method,
    credentials: 'same-origin', // send the session cookie on every call (it's httpOnly; default anyway)
    headers: { ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...headers },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return null;
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    /* non-JSON body — leave data null, fall through to the status line */
  }
  if (!res.ok) {
    const e = data?.error ?? data?.detail?.error; // JSONResponse shape OR HTTPException-nested
    throw new ApiError(res.status, e?.code, e?.message || `${res.status} ${res.statusText}`);
  }
  return data;
}

export const get = (path, opts) => api('GET', path, opts);
export const post = (path, body, opts) => api('POST', path, { ...opts, body });
export const patch = (path, body, opts) => api('PATCH', path, { ...opts, body });
export const put = (path, body, opts) => api('PUT', path, { ...opts, body });
export const del = (path, opts) => api('DELETE', path, opts);
