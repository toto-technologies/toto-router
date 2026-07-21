<script>
  // Model comparison picker — the trigger is a mini model card (name + residency + per-1M price
  // + context), the popover is a searchable price-sorted candidate list, and pinning 2–3 rows
  // flips it into side-by-side compare columns priced against THIS task type's observed traffic
  // (see $lib/traffic.js). Cold-start rule is hard: no traffic for the task type → static per-1M
  // prices only, never a fabricated estimate.
  import { prettyModel } from '$lib/models.js';
  import { taskLabel as fmtTask } from '$lib/tasks.js';
  import { perMtok, fmtMtok } from '$lib/pricing.js';
  import { ctxShort } from '$lib/catalog.js';
  import { monthlyCost, fmtMonthly } from '$lib/traffic.js';

  let {
    models = [],           // effective-catalog rows (/v1/admin/catalog/models shape)
    value = '',            // current catalog id ('' with noneLabel = the none row)
    onchange,              // (id) => void
    allowed = null,        // Set of pickable ids (null = all); others render dimmed + unselectable
    defaultId = null,      // policy default — trigger + rows tag "default" when it matches
    taskLabel = null,      // task type whose observed traffic prices the compare columns
    traffic = null,        // trafficStats() result {perModel, perLabel}, null while loading
    newer = null,          // Set of ids carrying the "newer · cheaper" tag
    noneLabel = null,      // optional '' row pinned first (e.g. "Default (platform classifier)")
    onviewcatalog = null,  // (id) => void — "View in catalog" hand-off (compare footer)
    ariaLabel = 'Choose model',
    disabled = false,
  } = $props();

  const byId = $derived(new Map(models.map((m) => [m.id, m])));
  const current = $derived(byId.get(value) ?? null);
  const isAllowed = (id) => !allowed || allowed.has(id);
  const isTest = (m) => m.endpoint === 'fake' || m.provider === 'fake';
  const blended = (m) => (m.price_in == null && m.price_out == null ? null : (m.price_in ?? 0) + (m.price_out ?? 0));
  const price2 = (m) => `${fmtMtok(perMtok(m.price_in))} · ${fmtMtok(perMtok(m.price_out))} /M`;
  const p50 = (id) => traffic?.perModel?.get(id)?.p50_ms ?? null;
  const labelStat = $derived(taskLabel ? (traffic?.perLabel?.get(taskLabel) ?? null) : null);

  let open = $state(false);
  let q = $state('');
  let active = $state(0);   // keyboard cursor into `flat`
  let pinned = $state([]);  // ids pinned for compare (2–3 flips the compare view)
  let listView = $state(false); // "back to list" keeps pins so a 3rd can join; a new pin re-flips
  let pos = $state({ top: 0, left: 0, maxH: 480 });
  let triggerEl, searchEl;

  // Blended price ascending; unpriced sink below priced; test (echo) models are a separate group
  // pinned LAST — never first, never a default.
  const sorted = $derived.by(() => {
    const key = (m) => blended(m) ?? Infinity;
    const real = models.filter((m) => !isTest(m)).sort((a, b) => key(a) - key(b) || prettyModel(a).localeCompare(prettyModel(b)));
    const test = models.filter(isTest).sort((a, b) => key(a) - key(b));
    return { real, test };
  });
  const match = (m) => {
    const needle = q.trim().toLowerCase();
    return !needle || `${prettyModel(m)} ${m.id}`.toLowerCase().includes(needle);
  };
  const shownReal = $derived(sorted.real.filter(match));
  const shownTest = $derived(sorted.test.filter(match));
  // Flat keyboard row list: the optional none row (null sentinel), then real, then test.
  const flat = $derived([...(noneLabel && !q.trim() ? [null] : []), ...shownReal, ...shownTest]);
  const compare = $derived(pinned.length >= 2 && !listView);
  const pinnedModels = $derived(pinned.map((id) => byId.get(id)).filter(Boolean));

  function place() {
    const r = triggerEl?.getBoundingClientRect();
    if (!r) return;
    const W = Math.min(560, window.innerWidth - 16);
    const left = Math.max(8, Math.min(r.left, window.innerWidth - W - 8));
    let top = r.bottom + 6;
    let maxH = window.innerHeight - top - 12;
    if (maxH < 260) { // not enough room below — open upward
      maxH = Math.min(480, r.top - 18);
      top = r.top - 6 - maxH;
    }
    pos = { top, left, maxH: Math.min(520, maxH) };
  }
  function openPanel() {
    if (disabled) return;
    place();
    q = '';
    active = flat.findIndex((m) => (m?.id ?? '') === value);
    if (active < 0) active = 0;
    open = true;
    queueMicrotask(() => searchEl?.focus());
  }
  function close(refocus = false) {
    open = false;
    pinned = [];
    listView = false;
    if (refocus) triggerEl?.focus();
  }
  function choose(m) {
    const id = m?.id ?? '';
    if (m && !isAllowed(m.id)) return;
    onchange?.(id);
    close(true);
  }
  function togglePin(id) {
    pinned = pinned.includes(id) ? pinned.filter((p) => p !== id) : [...pinned, id].slice(-3);
    listView = false; // a new pin (re-)enters compare once 2+ are held
  }

  function triggerKey(e) {
    if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown') {
      e.preventDefault();
      openPanel();
    }
  }
  // Escape rides the window: after a pin/back click the re-render can drop focus to <body>,
  // where a panel-scoped keydown would never hear it (and a Modal host must not see it either).
  function windowKey(e) {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(true); }
  }
  function panelKey(e) {
    if (e.key === 'Escape') return; // windowKey owns it
    if (compare) return; // compare view: tab between Choose buttons, Esc handled above
    const step = (dir) => {
      if (!flat.length) return;
      let i = active;
      for (let n = 0; n < flat.length; n++) {
        i = (i + dir + flat.length) % flat.length;
        if (!flat[i] || isAllowed(flat[i].id)) break; // skip denied rows
      }
      active = i;
      document.getElementById(rowId(i))?.scrollIntoView({ block: 'nearest' });
    };
    if (e.key === 'ArrowDown') { e.preventDefault(); step(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); step(-1); }
    else if (e.key === 'Enter') { e.preventDefault(); choose(flat[active]); }
  }
  const uid = `mp${Math.random().toString(36).slice(2, 8)}`;
  const rowId = (i) => `${uid}-row-${i}`;

  // pointerdown, not click: a click that mutates state (back-to-list, pin) detaches its target
  // before the window listener runs, and a detached node no longer matches the panel selector.
  function outside(e) {
    if (!open) return;
    if (e.target.closest?.(`.mp-panel[data-uid="${uid}"]`) || triggerEl?.contains(e.target)) return;
    close();
  }
</script>

<svelte:window
  onpointerdown={open ? outside : undefined}
  onkeydowncapture={open ? windowKey : undefined}
  onscroll={open ? place : undefined}
  onresize={open ? place : undefined}
/>

<button
  class="mp-trigger"
  class:unavail={!!value && !isAllowed(value)}
  bind:this={triggerEl}
  onclick={() => (open ? close() : openPanel())}
  onkeydown={triggerKey}
  aria-haspopup="listbox"
  aria-expanded={open}
  aria-label={ariaLabel}
  {disabled}
  type="button"
>
  <span class="mp-lines">
    {#if current}
      <span class="l1">
        <span class="nm">{prettyModel(current)}</span>
        {#if current.residency_class === 'in_perimeter'}
          <span class="chip perim"><span class="d"></span>in-perimeter</span>
        {:else}
          <span class="chip cloud"><span class="d"></span>cloud</span>
        {/if}
        {#if current.provider === 'local'}<span class="mp-tag">local</span>{/if}
        {#if current.source === 'adopted'}<span class="mp-tag">Added by you</span>{/if}
        {#if current.fine_tuned}<span class="mp-tag">Fine-tuned</span>{/if}
        {#if isTest(current)}<span class="mp-tag">test</span>{/if}
        {#if defaultId != null && value === defaultId}<span class="mp-tag dft">default</span>{/if}
      </span>
      <span class="l2 n">
        {#if blended(current) != null}{price2(current)}{:else}unpriced{/if}
        {#if current.context_window}&thinsp;·&thinsp;{ctxShort(current.context_window)} ctx{/if}
      </span>
    {:else if noneLabel && !value}
      <span class="l1"><span class="nm">{noneLabel}</span></span>
      <span class="l2 n">gateway-managed</span>
    {:else}
      <span class="l1"><span class="nm muted">Choose a model…</span></span>
    {/if}
  </span>
  <svg class="mp-caret" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6" /></svg>
</button>

{#if open}
  <div
    class="mp-panel"
    data-uid={uid}
    role="listbox"
    aria-label={ariaLabel}
    tabindex="-1"
    style="top:{pos.top}px;left:{pos.left}px;max-height:{pos.maxH}px"
    onkeydown={panelKey}
  >
    {#if !compare}
      <div class="mp-search">
        <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="M20 20l-3.5-3.5" /></svg>
        <input
          bind:this={searchEl}
          bind:value={q}
          placeholder="Search models…"
          aria-label="Search models"
          oninput={() => (active = 0)}
        />
        {#if pinned.length === 1}
          <span class="mp-pinhint">pin one more to compare</span>
        {:else if pinned.length >= 2}
          <button class="mp-cmpill" onclick={() => (listView = false)} type="button">Compare ({pinned.length}) ›</button>
        {/if}
      </div>
      <div class="mp-list">
        {#each flat as m, i (m?.id ?? '·none')}
          {@const denied = m && !isAllowed(m.id)}
          {@const first = m && shownTest.length && m === shownTest[0]}
          {#if first}<div class="mp-group">Test models — built-in echo, for verifying without spending</div>{/if}
          <div
            class="mp-row"
            class:active={i === active}
            class:denied
            class:sel={(m?.id ?? '') === value}
            id={rowId(i)}
            role="option"
            aria-selected={(m?.id ?? '') === value}
            aria-disabled={denied || undefined}
            onclick={() => !denied && choose(m)}
            onmousemove={() => (active = i)}
          >
            <span class="mp-check" aria-hidden="true">{(m?.id ?? '') === value ? '✓' : ''}</span>
            {#if m === null}
              <span class="mp-name">{noneLabel}</span>
              <span class="mp-facts n">gateway-managed</span>
            {:else}
              <span class="mp-name">
                {prettyModel(m)}
                <span class="mp-id n">{m.id}</span>
                {#if defaultId != null && m.id === defaultId}<span class="mp-tag dft">default</span>{/if}
                {#if newer?.has(m.id)}<span class="mp-tag newer">newer · cheaper</span>{/if}
                {#if m.source === 'adopted'}<span class="mp-tag">Added by you</span>{/if}
                {#if denied}<span class="mp-tag">unavailable</span>{/if}
              </span>
              <span class="mp-facts n">
                {#if blended(m) != null}{price2(m)}{:else}unpriced{/if}
                {#if m.context_window}&thinsp;·&thinsp;{ctxShort(m.context_window)}{/if}
              </span>
              {#if m.residency_class === 'in_perimeter'}
                <span class="chip perim"><span class="d"></span>in-perimeter</span>
              {:else}
                <span class="chip cloud"><span class="d"></span>cloud</span>
              {/if}
              {#if m.tools}
                <svg class="mp-glyph" viewBox="0 0 24 24" aria-label="supports tools"><path d="M14.5 6.5a4 4 0 0 0-5.6 4.9L4 16.3V20h3.7l4.9-4.9a4 4 0 0 0 4.9-5.6l-2.8 2.8-2.1-2.1z" /></svg>
              {/if}
              <button
                class="mp-pin"
                class:on={pinned.includes(m.id)}
                title={pinned.includes(m.id) ? 'Unpin' : 'Pin to compare (2–3 models)'}
                aria-label="Pin {prettyModel(m)} to compare"
                aria-pressed={pinned.includes(m.id)}
                onclick={(e) => { e.stopPropagation(); togglePin(m.id); }}
                type="button"
              >
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 4h6l-1 6 3 3v2h-4v5l-1 1-1-1v-5H7v-2l3-3z" /></svg>
              </button>
            {/if}
          </div>
        {:else}
          <div class="mp-empty">No models match “{q}”.</div>
        {/each}
      </div>
      <div class="mp-foot n">↑↓ move · Enter choose · Esc close · pin 2–3 to compare</div>
    {:else}
      <div class="mp-cmphead">
        <b>Compare</b>
        {#if taskLabel && labelStat}
          <span class="mp-thesis">
            your <b>{fmtTask(taskLabel)}</b> traffic at current volume —
            ~{Math.round(labelStat.perMonth)} req/mo · ~{Math.round(labelStat.avgTokensIn)} in /
            {Math.round(labelStat.avgTokensOut)} out tok/req
          </span>
        {:else if taskLabel}
          <span class="mp-thesis cold">no traffic yet for <b>{fmtTask(taskLabel)}</b> — priced per 1M tokens</span>
        {/if}
        <button class="mp-back" onclick={() => (listView = true)} type="button">‹ back to list</button>
      </div>
      <div class="mp-cols" style="--cols:{pinnedModels.length}">
        {#each pinnedModels as m (m.id)}
          {@const est = labelStat ? monthlyCost(labelStat, m) : null}
          <div class="mp-col" class:cur={m.id === value}>
            <div class="mp-colname">
              {prettyModel(m)}
              {#if newer?.has(m.id)}<span class="mp-tag newer">newer · cheaper</span>{/if}
            </div>
            <div class="mp-colid n">{m.id}</div>
            {#if est != null}
              <div class="mp-est">{fmtMonthly(est)}</div>
              <div class="mp-estsub">for {fmtTask(taskLabel)} at current volume</div>
            {:else}
              <div class="mp-est cold">{blended(m) == null ? 'unpriced' : price2(m)}</div>
              <div class="mp-estsub">{taskLabel ? 'no traffic yet — static price' : 'per 1M tokens'}</div>
            {/if}
            <dl>
              <div><dt>$ / 1M in·out</dt><dd class="n">{blended(m) == null ? '—' : `${fmtMtok(perMtok(m.price_in))} · ${fmtMtok(perMtok(m.price_out))}`}</dd></div>
              <div><dt>Context</dt><dd class="n">{ctxShort(m.context_window)}</dd></div>
              <div>
                <dt>Residency</dt>
                <dd>
                  {#if m.residency_class === 'in_perimeter'}
                    <span class="chip perim"><span class="d"></span>in-perimeter</span>
                  {:else}
                    <span class="chip cloud"><span class="d"></span>cloud</span>
                  {/if}
                </dd>
              </div>
              <div><dt>Tools</dt><dd>{m.tools ? 'yes' : 'no'}</dd></div>
              <div>
                <dt>p50 latency</dt>
                <dd class="n" title="Observed on this gateway's own traffic">
                  {p50(m.id) == null ? 'no traffic yet' : `${Math.round(p50(m.id))} ms`}
                </dd>
              </div>
            </dl>
            <button
              class="btn small primary mp-choose"
              disabled={!isAllowed(m.id)}
              onclick={() => choose(m)}
              type="button"
            >{m.id === value ? 'Keep' : 'Choose'}</button>
            {#if onviewcatalog}
              <button class="mp-viewcat" onclick={() => { close(); onviewcatalog(m.id); }} type="button">
                View in catalog ↓
              </button>
            {/if}
          </div>
        {/each}
      </div>
    {/if}
  </div>
{/if}

<style>
  .mp-trigger {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    min-width: 230px;
    max-width: 340px;
    text-align: left;
    padding: 5px 9px;
    background: var(--panel-2);
    border: 1px solid var(--line-2);
    border-radius: 8px;
    color: var(--text);
    cursor: pointer;
  }
  .mp-trigger:hover { border-color: var(--accent-line); }
  .mp-trigger:focus-visible { border-color: var(--accent-line); outline: 1px solid var(--accent-line); }
  .mp-trigger:disabled { opacity: 0.55; cursor: default; }
  .mp-trigger.unavail { border-color: rgba(213, 55, 66, 0.5); }
  .mp-lines { display: flex; flex-direction: column; gap: 1px; min-width: 0; flex: 1 1 auto; }
  .l1 { display: flex; align-items: center; gap: 6px; min-width: 0; flex-wrap: wrap; }
  .l1 .nm {
    font-size: 13px;
    font-weight: calc(500 + (var(--ui-weight) - 400));
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .l1 .nm.muted { color: var(--text-3); font-weight: inherit; }
  .l2 { font-size: 11.5px; color: var(--text-3); letter-spacing: -0.01em; }
  .n { font-family: var(--mono); }
  .mp-caret { width: 14px; height: 14px; flex: 0 0 auto; stroke: var(--text-3); fill: none; stroke-width: 1.7; }
  .mp-tag {
    font-size: 0.625rem;
    line-height: 1.5;
    padding: 0 6px;
    border-radius: 20px;
    border: 1px solid var(--line-2);
    color: var(--text-2);
    white-space: nowrap;
  }
  .mp-tag.dft { color: var(--accent); border-color: var(--accent-line); background: var(--accent-soft); }
  .mp-tag.newer { color: var(--good, var(--accent)); border-color: currentColor; }

  .mp-panel {
    position: fixed;
    z-index: 300;
    width: min(560px, calc(100vw - 16px));
    display: flex;
    flex-direction: column;
    background: var(--panel);
    border: 1px solid var(--line-2);
    border-radius: 11px;
    box-shadow: 0 12px 34px rgba(0, 0, 0, 0.18);
    overflow: hidden;
  }
  .mp-search {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 9px 12px;
    border-bottom: 1px solid var(--line);
    flex: 0 0 auto;
  }
  .mp-search svg { width: 14px; height: 14px; stroke: var(--text-3); fill: none; stroke-width: 1.7; }
  .mp-search input {
    flex: 1 1 auto;
    border: 0;
    background: transparent;
    color: var(--text);
    font-size: 0.8125rem;
    outline: none;
  }
  .mp-pinhint { font-size: 0.65625rem; color: var(--accent); white-space: nowrap; }
  .mp-cmpill {
    all: unset;
    cursor: pointer;
    font-size: 0.65625rem;
    font-weight: calc(600 + (var(--ui-weight) - 400));
    color: var(--accent);
    border: 1px solid var(--accent-line);
    background: var(--accent-soft);
    border-radius: 20px;
    padding: 2px 9px;
    white-space: nowrap;
  }
  .mp-cmpill:focus-visible { outline: 1px solid var(--accent-line); }
  .mp-list { overflow-y: auto; flex: 1 1 auto; padding: 4px; }
  .mp-group {
    margin: 6px 8px 2px;
    padding-top: 6px;
    border-top: 1px solid var(--line);
    font-size: 0.625rem;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    color: var(--text-3);
  }
  .mp-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 8px;
    border-radius: 7px;
    cursor: pointer;
    min-width: 0;
  }
  .mp-row.active { background: var(--panel-hi); }
  .mp-row.denied { opacity: 0.45; cursor: default; }
  .mp-check { width: 12px; flex: 0 0 auto; color: var(--accent); font-size: 0.75rem; }
  .mp-name {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
    flex: 1 1 auto;
    font-size: 0.8125rem;
    font-weight: calc(500 + (var(--ui-weight) - 400));
    white-space: nowrap;
    overflow: hidden;
  }
  .mp-row.sel .mp-name { font-weight: calc(620 + (var(--ui-weight) - 400)); }
  .mp-id { font-size: 0.625rem; color: var(--text-3); font-weight: 400; overflow: hidden; text-overflow: ellipsis; }
  .mp-facts { flex: 0 0 auto; font-size: 0.6875rem; color: var(--text-2); white-space: nowrap; }
  .mp-glyph { width: 13px; height: 13px; flex: 0 0 auto; stroke: var(--text-3); fill: none; stroke-width: 1.6; }
  .mp-pin {
    all: unset;
    cursor: pointer;
    width: 22px;
    height: 22px;
    border-radius: 6px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    color: var(--text-3);
    flex: 0 0 auto;
  }
  .mp-pin:hover { background: var(--panel-2); color: var(--text); }
  .mp-pin:focus-visible { outline: 1px solid var(--accent-line); }
  .mp-pin.on { color: var(--accent); background: var(--accent-soft); }
  .mp-pin svg { width: 13px; height: 13px; stroke: currentColor; fill: none; stroke-width: 1.6; }
  .mp-empty { padding: 18px 12px; text-align: center; color: var(--text-3); font-size: 0.78125rem; }
  .mp-foot {
    flex: 0 0 auto;
    padding: 6px 12px;
    border-top: 1px solid var(--line);
    font-size: 0.625rem;
    color: var(--text-3);
  }

  .mp-cmphead {
    display: flex;
    align-items: baseline;
    gap: 10px;
    padding: 10px 14px;
    border-bottom: 1px solid var(--line);
    flex-wrap: wrap;
  }
  .mp-cmphead b { font-size: 0.8125rem; }
  .mp-thesis { font-size: 0.71875rem; color: var(--text-2); }
  .mp-thesis.cold { color: var(--text-3); }
  .mp-back {
    all: unset;
    cursor: pointer;
    margin-left: auto;
    font-size: 0.71875rem;
    color: var(--accent);
  }
  .mp-back:focus-visible { outline: 1px solid var(--accent-line); }
  .mp-cols {
    display: grid;
    grid-template-columns: repeat(var(--cols), minmax(0, 1fr));
    gap: 1px;
    background: var(--line);
    overflow-y: auto;
  }
  .mp-col { background: var(--panel); padding: 12px 14px; display: flex; flex-direction: column; min-width: 0; }
  .mp-col.cur { background: color-mix(in oklab, var(--accent-soft) 30%, var(--panel)); }
  .mp-colname {
    font-size: 0.8125rem;
    font-weight: calc(620 + (var(--ui-weight) - 400));
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
  }
  .mp-colid { font-size: 0.625rem; color: var(--text-3); margin-top: 1px; }
  .mp-est {
    margin-top: 10px;
    font-family: var(--mono);
    font-size: 1.05rem;
    font-weight: calc(600 + (var(--ui-weight) - 400));
    color: var(--text);
  }
  .mp-est.cold { font-size: 0.8125rem; color: var(--text-2); }
  .mp-estsub { font-size: 0.625rem; color: var(--text-3); margin-bottom: 8px; }
  .mp-col dl { margin: 0; display: flex; flex-direction: column; }
  .mp-col dl > div {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 8px;
    padding: 5px 0;
    border-top: 1px solid var(--line);
    font-size: 0.71875rem;
  }
  .mp-col dt { color: var(--text-3); }
  .mp-col dd { margin: 0; color: var(--text); text-align: right; }
  .mp-choose { margin-top: 10px; justify-content: center; }
  .mp-viewcat {
    all: unset;
    cursor: pointer;
    margin-top: 8px;
    font-size: 0.6875rem;
    color: var(--accent);
    text-align: center;
  }
  .mp-viewcat:focus-visible { outline: 1px solid var(--accent-line); }
</style>
