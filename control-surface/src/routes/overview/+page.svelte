<script>
  // Overview — customizable widget dashboard (docs/design/2026-07-12-overview-dashboard-brief.md).
  // This file owns the shell: pagehead (greeting + status + range + Customize), the 12-col grid,
  // edit mode, pointer/keyboard drag-reorder, and the layout store. Widgets own their own data.
  import { setContext } from 'svelte';
  import { flip } from 'svelte/animate';
  import { fly } from 'svelte/transition';
  import { cubicOut } from 'svelte/easing';
  import { getMe, getOrg, getProviderHealth, getUsage } from '$lib/api/admin.js';
  import { query } from '$lib/api/resource.svelte.js';
  import Card from '$lib/components/Card.svelte';
  import SegmentedControl from '$lib/components/SegmentedControl.svelte';
  import SkeletonCard from '$lib/components/SkeletonCard.svelte';
  import { WIDGETS, rangeLabel } from '$lib/overview/registry.js';
  import { createLayout } from '$lib/overview/layout.svelte.js';
  import { headerHealthClause } from '$lib/overview/telemetry.js';

  const reduced = () =>
    typeof matchMedia !== 'undefined' && matchMedia('(prefers-reduced-motion: reduce)').matches;

  const byId = Object.fromEntries(WIDGETS.map((w) => [w.id, w]));

  // ---- page-global range (feeds every range-aware widget via prop) ------------------------------
  const RANGES = { '24h': 24 * 3600e3, '7d': 7 * 864e5, '30d': 30 * 864e5 };
  let range = $state('24h');
  const rangeStart = (r) => new Date(Date.now() - RANGES[r]).toISOString();

  // Header spend clause + the page auth gate. W1 fetches its own grouped slice, so this flat
  // slice is a small duplicate call — fold both into a shared usage resource if it ever bothers.
  const OSS = typeof __EDITION__ !== 'undefined' && __EDITION__ === 'oss';

  const usage = query(() => getUsage({ start: rangeStart(range) }));
  const me = query(() => getMe());
  // Org identity is enterprise-only; single-tenant builds skip the fetch and the layout
  // store namespaces under the empty org key.
  const org = query(() => getOrg(), { immediate: !OSS });
  function onRange(r) {
    range = r;
    usage.reload();
  }

  // Page-level auth dead-ends key off the usage slice (same gate as the old page).
  const gate = $derived(usage.status === 'unauthed' || usage.status === 'forbidden' ? usage.status : null);

  // ---- header greeting + status sentence ---------------------------------------------------------
  const hour = new Date().getHours();
  const tod = hour < 12 ? 'morning' : hour < 18 ? 'afternoon' : 'evening';
  // First name from the signed-in email's local part ("alex.funk@…" → "Alex"); operator has none.
  const greetName = $derived.by(() => {
    if (me.status !== 'ok' || me.data?.is_operator) return '';
    const w = (me.data?.email ?? '').split('@')[0].split(/[.\-_]/).filter(Boolean)[0] ?? '';
    return w ? w[0].toUpperCase() + w.slice(1) : '';
  });
  const spendReady = $derived(usage.status === 'ok' || usage.status === 'empty');
  const spend = $derived((usage.data?.rows ?? []).reduce((s, r) => s + (+r.cost_usd || 0), 0));
  // Whole dollars below $10k, "$12.4k" above (brief §4 number rules).
  const usd = (n) => (n >= 10000 ? '$' + (n / 1000).toFixed(1) + 'k' : '$' + Math.round(n || 0).toLocaleString('en-US'));
  // One page-level health fetch feeds the clause (the widget owns its own 30s poll);
  // non-admin / error / no providers → the clause is omitted, the sentence still stands.
  const health = query(() => getProviderHealth({}), { isEmpty: (d) => !d?.providers?.length });
  const healthClause = $derived(health.status === 'ok' ? headerHealthClause(health.data.providers) : null);

  // ---- layout store — created once org identity resolves so persistence is org-namespaced -------
  let layout = $state(null);
  $effect(() => {
    if (!layout && (OSS || org.status !== 'loading')) layout = createLayout(WIDGETS, org.data?.org_id ?? '');
  });

  // ---- edit mode + aria announcements ------------------------------------------------------------
  let editing = $state(false);
  let announce = $state('');
  function toggleEdit() {
    if (editing && kbGrab) dropKb(kbGrab); // leaving edit mode drops a keyboard grab in place
    editing = !editing;
  }
  const positionMsg = (id) => {
    const v = layout.visible;
    return `Moved ${byId[id].title} to position ${v.indexOf(id) + 1} of ${v.length}`;
  };

  // ---- keyboard reorder (brief §3.4): Enter/Space grabs, arrows move one slot, Esc cancels -------
  let kbGrab = $state(null);
  let kbSnapshot = null;
  function dropKb(id) {
    kbGrab = null;
    kbSnapshot = null;
    announce = `${byId[id].title} dropped at position ${layout.visible.indexOf(id) + 1}`;
  }
  function handleKey(id, e) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      if (kbGrab === id) dropKb(id);
      else {
        kbGrab = id;
        kbSnapshot = [...layout.order];
        announce = `${byId[id].title} grabbed — arrow keys move, Enter drops, Escape cancels`;
      }
    } else if (kbGrab === id && e.key === 'Escape') {
      e.preventDefault();
      layout.restoreOrder(kbSnapshot);
      kbGrab = null;
      kbSnapshot = null;
      announce = 'Reorder cancelled';
    } else if (kbGrab === id && e.key.startsWith('Arrow')) {
      e.preventDefault();
      const vis = layout.visible;
      const j = vis.indexOf(id) + (e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 1);
      if (j < 0 || j >= vis.length) return;
      layout.reorder(id, vis[j]);
      announce = positionMsg(id);
    }
  }

  // ---- pointer drag (brief §3.4): transform-only lift + live reorder + settle --------------------
  const cellEls = {}; // id → grid cell element (read in handlers only, deliberately not reactive)
  function cellRef(node, id) {
    cellEls[id] = node;
    return { destroy: () => { if (cellEls[id] === node) delete cellEls[id]; } };
  }
  let drag = $state(null); // { id, dx, dy, settling }
  let pd = null; // pre-lift pointer bookkeeping: { id, x0, y0, offX, offY, snapshot }
  let lastReorder = 0;

  function handleDown(id, e) {
    if (e.button !== 0 || kbGrab || e.target.closest('.wctl')) return;
    e.preventDefault();
    pd = { id, x0: e.clientX, y0: e.clientY, offX: 0, offY: 0, snapshot: [...layout.order] };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', onUp);
    window.addEventListener('keydown', onDragKey);
  }
  function onMove(e) {
    if (!pd) return;
    if (!drag) {
      if (Math.hypot(e.clientX - pd.x0, e.clientY - pd.y0) < 4) return; // 4px slop before lift
      const r = cellEls[pd.id]?.getBoundingClientRect();
      if (!r) return;
      pd.offX = pd.x0 - r.left;
      pd.offY = pd.y0 - r.top;
      drag = { id: pd.id, dx: 0, dy: 0, settling: false };
    }
    follow(e);
    maybeReorder(e);
    follow(e); // re-glue to the pointer after a live reorder moved the cell's base slot
  }
  // The transform lives on the inner .wslot, so the cell's rect is always the untransformed
  // base slot — dx/dy = pointer minus grab offset minus wherever the base slot currently is.
  function follow(e) {
    const r = cellEls[drag.id]?.getBoundingClientRect();
    if (!r) return;
    drag.dx = e.clientX - pd.offX - r.left;
    drag.dy = e.clientY - pd.offY - r.top;
  }
  // Drop target = the sibling whose midpoint the pointer crossed (in the direction of travel);
  // reordering live makes siblings FLIP into place while dragging.
  function maybeReorder(e) {
    if (performance.now() - lastReorder < 120) return; // let sibling FLIPs land before re-testing
    const vis = layout.visible;
    const fromIdx = vis.indexOf(drag.id);
    for (const id of vis) {
      if (id === drag.id) continue;
      const r = cellEls[id]?.getBoundingClientRect();
      if (!r || e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) continue;
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      const toIdx = vis.indexOf(id);
      const crossed = toIdx > fromIdx ? e.clientY > cy || e.clientX > cx : e.clientY < cy || e.clientX < cx;
      if (crossed) {
        layout.reorder(drag.id, id);
        announce = positionMsg(drag.id);
        lastReorder = performance.now();
      }
      break;
    }
  }
  function onUp() {
    finishDrag(false);
  }
  function onDragKey(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      finishDrag(true); // cancel: restore order, spring the card home
    }
  }
  function finishDrag(cancel) {
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
    window.removeEventListener('pointercancel', onUp);
    window.removeEventListener('keydown', onDragKey);
    const p = pd;
    pd = null;
    if (!drag) return; // never lifted — a plain tap on the header
    if (cancel && p) layout.restoreOrder(p.snapshot);
    if (reduced()) {
      drag = null; // reduced motion: reposition instantly, no settle tween
      return;
    }
    drag.settling = true; // CSS transitions transform → 0 over 380ms with the overshoot curve
    drag.dx = 0;
    drag.dy = 0;
    const id = drag.id;
    setTimeout(() => {
      if (drag?.id === id) drag = null;
    }, 400);
  }

  // WidgetFrame reads edit state + controls through this context (see WidgetFrame.svelte).
  setContext('overview-grid', {
    get editing() {
      return editing;
    },
    get grabbedId() {
      return kbGrab ?? drag?.id ?? null;
    },
    canResize: (id) => byId[id].sizes.length > 1,
    sizeOf: (id) => layout?.sizes[id],
    toggleSize: (id) => layout.toggleSize(id),
    // ponytail: size toggle reflows instantly — the sanctioned sibling-FLIP + cross-fade
    // (brief §5) needs manual FLIP outside the each-block's order tracking; add if it grates.
    hide: (id) => {
      layout.hide(id);
      announce = `${byId[id].title} hidden`;
    },
    handleDown,
    handleKey
  });

  // ---- motion -------------------------------------------------------------------------------------
  // JS cubic-bezier solver — flip/fly need a JS curve; CSS bezier strings only work in transitions.
  function bezier(x1, y1, x2, y2) {
    const at = (t, a, b) => 3 * t * (1 - t) * (1 - t) * a + 3 * t * t * (1 - t) * b + t * t * t;
    return (x) => {
      let lo = 0, hi = 1, t = x;
      for (let i = 0; i < 20; i++) {
        const cur = at(t, x1, x2);
        if (Math.abs(cur - x) < 1e-4) break;
        if (cur < x) lo = t; else hi = t;
        t = (lo + hi) / 2;
      }
      return at(t, y1, y2);
    };
  }
  const siblingEase = bezier(0.16, 1, 0.3, 1); // displaced cards: inert, no overshoot

  // Entrance stagger (brief §5): fly y:8→0 + fade, 150ms cubicOut, 40ms by grid order, ≤450ms total.
  const enter = (node, { i = 0 } = {}) =>
    fly(node, {
      y: reduced() ? 0 : 8,
      duration: reduced() ? 0 : 150,
      delay: reduced() ? 0 : Math.min(i * 40, 300),
      easing: cubicOut
    });

  // animate:flip for reorders — the dragged cell is excluded (it follows the pointer instead).
  function gridFlip(node, fromTo, params) {
    if (drag && node === cellEls[drag.id]) return { duration: 0 };
    return flip(node, fromTo, { duration: reduced() ? 0 : 250, easing: siblingEase });
  }
</script>

<svelte:head><title>Overview · Toto Control</title></svelte:head>

<div class="pagehead">
  <div>
    {#if greetName}
      <h1 class="hero-serif greet">Good {tod}, {greetName}.</h1>
    {:else}
      <h1>Overview</h1>
    {/if}
    <div class="statusline">
      {#if org.data?.name}<span>{org.data.name}</span>{/if}
      {#if healthClause}<span class="hc {healthClause.cls}">{healthClause.text}</span>{/if}
      {#if spendReady}<span><span class="num">{usd(spend)}</span> spent in the {rangeLabel(range)}</span>{/if}
    </div>
  </div>
  <div class="right">
    <SegmentedControl options={['24h', '7d', '30d']} value={range} onchange={onRange} />
    {#if editing}
      <button class="btn primary" onclick={toggleEdit}>Done</button>
    {:else}
      <button class="btn ghost" onclick={toggleEdit}>Customize</button>
    {/if}
  </div>
</div>

{#if gate === 'unauthed'}
  <Card><div class="deadend"><b>Sign in required</b><p>Your session has expired. Sign in to view org usage.</p></div></Card>
{:else if gate === 'forbidden'}
  <Card><div class="deadend"><b>Admin access needed</b><p>Overview shows org-wide usage — available to admins and owners.</p></div></Card>
{:else}
  {#if editing && layout?.hidden.length}
    <div class="tray">
      <span class="eyebrow">Hidden</span>
      {#each layout.hidden as id (id)}
        <button class="chip" onclick={() => { layout.show(id); announce = `${byId[id].title} shown`; }}>+ {byId[id].title}</button>
      {/each}
    </div>
  {/if}

  {#if !layout}
    <div class="wgrid">
      {#each Array(4) as _}<div class="cell lg"><SkeletonCard lines={3} /></div>{/each}
    </div>
  {:else}
    <div class="wgrid" class:isdragging={!!drag}>
      {#each layout.visible as id, i (id)}
        {@const Widget = byId[id].component}
        <div
          class="cell {layout.sizes[id]}"
          class:dragcell={drag?.id === id}
          use:cellRef={id}
          animate:gridFlip
          in:enter={{ i }}
        >
          {#if drag?.id === id}<div class="dropghost"></div>{/if}
          <div
            class="wslot"
            class:lifted={drag?.id === id && !drag.settling}
            class:settling={drag?.id === id && drag.settling}
            style:transform={drag?.id === id
              ? `translate3d(${drag.dx}px, ${drag.dy}px, 0)${drag.settling ? '' : ' scale(1.01)'}`
              : ''}
          >
            <Widget {range} size={layout.sizes[id]} />
          </div>
        </div>
      {/each}
    </div>
  {/if}
{/if}

<div class="srlive" aria-live="polite">{announce}</div>

<style>
  .greet { margin: 0; font-size: 1.625rem; font-weight: 400; letter-spacing: -0.01em; color: var(--text); }
  .statusline { display: flex; align-items: baseline; flex-wrap: wrap; gap: 3px 6px; margin-top: 3px; font-size: 0.78125rem; color: var(--text-2); }
  .statusline > span + span::before { content: '·'; margin-right: 6px; color: var(--text-3); }
  .statusline .hc.warn { color: var(--warn); }
  .statusline .hc.crit { color: var(--crit); }

  /* hidden-widget tray — edit mode only (brief §3.4) */
  .tray { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
  .tray .chip { cursor: pointer; background: var(--panel); }
  .tray .chip:hover { border-color: var(--accent-line); color: var(--accent); }

  /* 12-col substrate: sm = span 6, lg = span 12; single column ≤1080px (brief §3.1/§3.5) */
  .wgrid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 14px; }
  .cell { position: relative; min-width: 0; }
  .cell.sm { grid-column: span 6; }
  .cell.lg { grid-column: span 12; }
  @media (max-width: 1080px) {
    .cell.sm, .cell.lg { grid-column: span 12; }
  }

  /* drag chrome (brief §3.4): the dropghost marks the vacated base slot; the lifted card floats
     above. NOT named .ghost — that would also match the Customize button's `.btn.ghost`. */
  .wgrid.isdragging { user-select: none; cursor: grabbing; }
  .cell.dragcell { z-index: 5; }
  .dropghost { position: absolute; inset: 0; border: 1.5px dashed var(--accent-line); border-radius: 11px; background: var(--accent-soft); }
  .wslot { position: relative; height: 100%; border-radius: 11px; }
  .wslot.lifted { box-shadow: 0 4px 6px rgba(30, 48, 24, 0.2), 0 22px 44px rgba(30, 48, 24, 0.24); }
  @media (prefers-color-scheme: dark) {
    :global(:root:not([data-theme='light'])) .wslot.lifted { box-shadow: 0 4px 6px rgba(0, 0, 0, 0.35), 0 22px 44px rgba(0, 0, 0, 0.4); }
  }
  :global(:root[data-theme='dark']) .wslot.lifted { box-shadow: 0 4px 6px rgba(0, 0, 0, 0.35), 0 22px 44px rgba(0, 0, 0, 0.4); }
  /* dropped card settles into the ghost slot with a slight overshoot (brief §5) */
  .wslot.settling { transition: transform 380ms cubic-bezier(0.3, 1.25, 0.4, 1); }

  /* page-level auth dead-ends (unchanged from the old page) */
  .deadend { display: flex; flex-direction: column; align-items: flex-start; gap: 7px; padding: 8px 2px; }
  .deadend b { font-size: 0.875rem; }
  .deadend p { margin: 0; font-size: 0.78125rem; color: var(--text-2); }

  /* visually hidden aria-live region for reorder announcements */
  .srlive { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
</style>
