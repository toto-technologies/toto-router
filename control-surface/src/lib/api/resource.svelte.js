// The fetch-state primitive every Wave-2 page uses. Runs an admin call and exposes ONE reactive
// `status` a page renders distinct branches off:
//
//   loading   -> <Skeleton/>            (also the prerender/SSR state — calls only run in the browser)
//   ok        -> render r.data
//   empty     -> "nothing here yet"     (200 but no rows — a real, non-error state)
//   unauthed  -> 401: not signed in     -> login / redirect
//   forbidden -> 403: role too low      -> clean "needs admin" state
//   error     -> anything else          -> r.error.message (the gateway's {error:{message}} text)
//
// 401 and 403 are split into their OWN statuses (not lumped as `error`) so a page shows the right
// dead-end for each — that's the UI-3 requirement. Usage:
//
//   import { query } from '$lib/api/resource.svelte.js';
//   import { listTeams } from '$lib/api/admin.js';
//   const teams = query(() => listTeams());
//   {#if teams.status === 'loading'}<Skeleton/>{:else if teams.status === 'forbidden'} … {/if}
//
import { browser } from '$app/environment';
import { ApiError } from './client.js';

/** True when a 200 payload carries no rows — treated as `empty`, not `ok`. Covers the common admin
 *  shapes (arrays, and the {teams|members|invitations|events|rows|line_items:[…]} envelopes). */
function looksEmpty(data) {
  if (data == null) return true;
  if (Array.isArray(data)) return data.length === 0;
  for (const k of ['teams', 'members', 'invitations', 'events', 'rows', 'line_items', 'labels', 'tokens']) {
    if (Array.isArray(data[k])) return data[k].length === 0;
  }
  return false;
}

/**
 * @template T
 * @param {() => Promise<T>} fetcher  an admin.js call, e.g. `() => listTeams()`
 * @param {{ isEmpty?: (data: T) => boolean, immediate?: boolean }} [opts]
 *   isEmpty overrides the default emptiness check; immediate:false defers the first load to reload().
 * @returns {{ status: 'loading'|'ok'|'empty'|'unauthed'|'forbidden'|'error', data: T|null, error: (Error & {status?:number, code?:string})|null, reload: () => Promise<void> }}
 */
export function query(fetcher, { isEmpty = looksEmpty, immediate = true } = {}) {
  const r = $state({ status: 'loading', data: null, error: null });

  async function reload() {
    r.status = 'loading';
    r.error = null;
    try {
      const data = await fetcher();
      r.data = data;
      r.status = isEmpty(data) ? 'empty' : 'ok';
    } catch (e) {
      r.error = e;
      const s = e instanceof ApiError ? e.status : undefined;
      r.status = s === 401 ? 'unauthed' : s === 403 ? 'forbidden' : 'error';
    }
  }

  // Never fetch during prerender/SSR — the console is adapter-static; calls belong to the browser.
  if (browser && immediate) reload();
  r.reload = reload;
  return r;
}
