// Overview widget-grid layout store — { order, hidden, sizes } persisted to localStorage,
// namespaced per org so two orgs in one browser don't share a layout. Svelte 5 runes only.
// The pure helpers (defaults/sanitize/load/save) are exported for node --test — they never
// touch runes, so tests import this module without the Svelte compiler.
// ponytail: per-browser persistence; move server-side (per-user KV) when cross-device matters.

const KEY = 'toto.overview.layout.v1';

/** localStorage key for an org's layout ('' → the un-namespaced fallback key). */
export const storageKey = (orgId = '') => (orgId ? `${KEY}:${orgId}` : KEY);

/** Registry-order defaults. `widgets` = descriptor list ({id, sizes, defaultSize}). */
export function defaultLayout(widgets) {
  return {
    order: widgets.map((w) => w.id),
    hidden: [],
    sizes: Object.fromEntries(widgets.map((w) => [w.id, w.defaultSize]))
  };
}

/** Tolerant loader: drop unknown ids, append missing ids in registry order, and coerce each
 *  size to one the widget actually supports — stored state from an older/newer build never
 *  breaks the grid. Garbage in → defaults out. */
export function sanitize(raw, widgets) {
  const d = defaultLayout(widgets);
  if (!raw || typeof raw !== 'object') return d;
  const known = new Set(d.order);
  const order = (Array.isArray(raw.order) ? raw.order : []).filter((id) => known.has(id));
  for (const id of d.order) if (!order.includes(id)) order.push(id);
  const hidden = (Array.isArray(raw.hidden) ? raw.hidden : []).filter((id) => known.has(id));
  const sizes = {};
  for (const w of widgets) {
    const s = raw.sizes?.[w.id];
    sizes[w.id] = w.sizes.includes(s) ? s : w.defaultSize;
  }
  return { order, hidden, sizes };
}

export function loadLayout(widgets, orgId = '') {
  try {
    return sanitize(JSON.parse(globalThis.localStorage?.getItem(storageKey(orgId)) ?? 'null'), widgets);
  } catch {
    return defaultLayout(widgets);
  }
}

export function saveLayout(layout, orgId = '') {
  try {
    globalThis.localStorage?.setItem(
      storageKey(orgId),
      JSON.stringify({ order: layout.order, hidden: layout.hidden, sizes: layout.sizes })
    );
  } catch {
    /* storage full / disabled — layout just won't persist */
  }
}

/**
 * The reactive store the Overview page drives. Every mutation persists immediately.
 * @param {Array<{id: string, sizes: string[], defaultSize: string}>} widgets registry descriptors
 * @param {string} [orgId] namespace for persistence
 */
export function createLayout(widgets, orgId = '') {
  const l = $state(loadLayout(widgets, orgId));
  const persist = () => saveLayout(l, orgId);
  const byId = new Map(widgets.map((w) => [w.id, w]));

  return {
    get order() { return l.order; },
    get hidden() { return l.hidden; },
    get sizes() { return l.sizes; },
    /** Render order for the grid — `order` minus hidden. */
    get visible() { return l.order.filter((id) => !l.hidden.includes(id)); },
    /** Place `fromId` at `toId`'s position (splice-out then splice-in — moving forward lands
     *  after the target, backward lands before, which is the live-reorder swap feel). */
    reorder(fromId, toId) {
      const from = l.order.indexOf(fromId);
      const to = l.order.indexOf(toId);
      if (from < 0 || to < 0 || from === to) return;
      l.order.splice(from, 1);
      l.order.splice(to, 0, fromId);
      persist();
    },
    /** Restore a snapshot of `order` (Escape-cancel mid-drag). */
    restoreOrder(order) {
      l.order = [...order];
      persist();
    },
    hide(id) {
      if (!l.hidden.includes(id)) l.hidden.push(id);
      persist();
    },
    /** Re-add a hidden widget at the end (brief §3.4: tray chips append). */
    show(id) {
      l.hidden = l.hidden.filter((h) => h !== id);
      const i = l.order.indexOf(id);
      if (i >= 0) l.order.splice(i, 1);
      l.order.push(id);
      persist();
    },
    /** Flip sm↔lg for widgets that support both sizes; no-op otherwise. */
    toggleSize(id) {
      const w = byId.get(id);
      if (!w || w.sizes.length < 2) return;
      l.sizes[id] = l.sizes[id] === 'lg' ? 'sm' : 'lg';
      persist();
    },
    reset() {
      const d = defaultLayout(widgets);
      l.order = d.order;
      l.hidden = d.hidden;
      l.sizes = d.sizes;
      persist();
    }
  };
}
