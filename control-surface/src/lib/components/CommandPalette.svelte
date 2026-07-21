<script>
  // The topbar search, wired: typing filters a small palette of console pages + catalog models;
  // Enter (or click) navigates — pages to their route, models to their anchor row on the Catalog
  // page. No backend of its own: pages come from NAV_GROUPS, models from the effective catalog
  // (the same list the routing pickers use), fetched once on first focus.
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { NAV_GROUPS } from '$lib/nav.js';
  import { getEffectiveModels } from '$lib/api/admin.js';
  import { prettyModel } from '$lib/models.js';

  let { placeholder = 'Search…' } = $props();

  const pages = NAV_GROUPS.flatMap((g) => g.items);
  let el = $state(null);
  let q = $state('');
  let open = $state(false);
  let sel = $state(0);
  let models = $state(null); // null = not fetched yet; [] = fetching/none
  function ensureModels() {
    if (models !== null) return;
    models = [];
    getEffectiveModels()
      .then((r) => (models = r.models ?? []))
      .catch(() => {}); // no models in the palette is a fine degraded state
  }

  const results = $derived.by(() => {
    const s = q.trim().toLowerCase();
    if (!s) return [];
    const pageHits = pages
      .filter((p) => p.label.toLowerCase().includes(s))
      .map((p) => ({ kind: 'page', label: p.label, href: p.href }));
    const modelHits = (models ?? [])
      .filter((m) => m.id.toLowerCase().includes(s) || prettyModel(m.id).toLowerCase().includes(s))
      .slice(0, 6)
      .map((m) => ({ kind: 'model', label: prettyModel(m.id), sub: m.id, href: `/catalog#cat-${m.id}` }));
    return [...pageHits, ...modelHits].slice(0, 8);
  });
  $effect(() => {
    void results;
    sel = 0;
  });

  function close() {
    open = false;
    q = '';
    el?.blur();
  }
  function pick(r) {
    close();
    goto(base + r.href);
  }
  function onKeydown(e) {
    if (e.key === 'Escape') close();
    else if (e.key === 'ArrowDown') {
      e.preventDefault();
      sel = Math.min(sel + 1, results.length - 1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      sel = Math.max(sel - 1, 0);
    } else if (e.key === 'Enter' && results[sel]) pick(results[sel]);
  }

  /** ⌘K target — the layout's global shortcut calls this. */
  export function focus() {
    el?.focus();
  }
</script>

<div class="search" role="combobox" aria-haspopup="listbox" aria-expanded={open && results.length > 0}>
  <svg class="ico" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7" /><path d="M21 21l-4-4" /></svg>
  <input
    bind:this={el}
    bind:value={q}
    {placeholder}
    aria-label="Search"
    aria-autocomplete="list"
    onfocus={() => {
      ensureModels();
      open = true;
    }}
    oninput={() => (open = true)}
    onblur={() => (open = false)}
    onkeydown={onKeydown} />
  <span class="kbd">⌘K</span>
  {#if open && results.length > 0}
    <div class="pal" role="listbox">
      {#each results as r, i}
        <!-- mousedown (not click) so the input's blur doesn't close the list first -->
        <div
          class="row"
          class:on={i === sel}
          role="option"
          aria-selected={i === sel}
          onmousedown={(e) => {
            e.preventDefault();
            pick(r);
          }}>
          <span class="k">{r.kind}</span>
          <span class="l">{r.label}</span>
          {#if r.sub}<span class="s">{r.sub}</span>{/if}
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .search {
    position: relative;
  }
  .pal {
    position: absolute;
    top: calc(100% + 6px);
    left: 0;
    right: 0;
    background: var(--panel);
    border: 1px solid var(--line-2);
    border-radius: 10px;
    padding: 5px;
    box-shadow: var(--shadow);
    z-index: 50;
  }
  .row {
    display: flex;
    align-items: baseline;
    gap: 9px;
    padding: 7px 9px;
    border-radius: 7px;
    cursor: pointer;
    font-size: 0.8125rem;
    min-width: 0;
  }
  .row.on,
  .row:hover {
    background: var(--panel-hi);
  }
  .row .k {
    flex: 0 0 auto;
    font-size: 0.625rem;
    letter-spacing: 0.09em;
    text-transform: uppercase;
    color: var(--text-3);
    width: 44px;
  }
  .row .l {
    color: var(--text);
    white-space: nowrap;
  }
  .row .s {
    font-family: var(--mono);
    font-size: 0.71875rem;
    color: var(--text-3);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
