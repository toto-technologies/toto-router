<script>
  // Shared widget chrome (brief §4 common grammar): .card with .ch header (plain-language h3
  // + .meta qualifier), the widget body, and a whole-row footer deep link. Edit-mode chrome
  // (drag handle, size toggle, hide) is driven by the Overview page through the
  // 'overview-grid' context — outside the dashboard the frame renders as a plain card.
  import { getContext } from 'svelte';
  import { base } from '$app/paths';

  let { id = '', title = '', meta = '', href = '', linkLabel = '', metaSnippet, children } = $props();

  const ctl = getContext('overview-grid');
  const editing = $derived(ctl?.editing ?? false);
  const grabbed = $derived(ctl?.grabbedId === id);
  const canResize = $derived(editing && (ctl?.canResize(id) ?? false));
</script>

<div class="card wframe" class:editing class:grabbed>
  <!-- pointer drag works from the whole header (matches the grab cursor); the keyboard path
       lives on the handle button below, so this div-level listener is pointer-only redundancy -->
  <div class="ch" role="presentation" onpointerdown={editing ? (e) => ctl.handleDown(id, e) : undefined}>
    {#if editing}
      <!-- keyboard reorder lives on the handle: Enter/Space grabs, arrows move, Esc cancels -->
      <button
        class="whandle"
        aria-pressed={grabbed}
        aria-label="Move {title} widget — press Enter to grab, arrow keys to move"
        onkeydown={(e) => ctl.handleKey(id, e)}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          {#each [5, 12, 19] as y}<circle cx="9" cy={y} r="1.6" /><circle cx="15" cy={y} r="1.6" />{/each}
        </svg>
      </button>
    {/if}
    <h3>{title}</h3>
    {#if metaSnippet}{@render metaSnippet()}{:else if meta}<span class="meta">{meta}</span>{/if}
    {#if editing}
      <span class="wctls">
        {#if canResize}
          <button class="wctl" title="Resize ({ctl.sizeOf(id) === 'lg' ? 'make smaller' : 'make larger'})" aria-label="Resize {title}" onclick={() => ctl.toggleSize(id)}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="5" width="17" height="8" rx="1.5" /><rect x="3.5" y="16" width="9" height="4.5" rx="1.5" /></svg>
          </button>
        {/if}
        <button class="wctl" title="Hide widget" aria-label="Hide {title}" onclick={() => ctl.hide(id)}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 3l18 18M10.5 5.2A9.8 9.8 0 0 1 12 5c6.5 0 10 7 10 7a17.4 17.4 0 0 1-3.2 4M6.6 6.6C3.8 8.6 2 12 2 12s3.5 7 10 7a9.5 9.5 0 0 0 4.3-1M9.9 9.9a3 3 0 0 0 4.2 4.2" /></svg>
        </button>
      </span>
    {/if}
  </div>
  <div class="cb">{@render children?.()}</div>
  {#if href}
    <a class="wfoot" href={base + href}>
      <span>{linkLabel}</span><span class="arr" aria-hidden="true">→</span>
    </a>
  {/if}
</div>

<style>
  /* card fills its grid cell so side-by-side sm widgets share a row height */
  .wframe { display: flex; flex-direction: column; height: 100%; }
  .wframe .cb { flex: 1; }

  /* footer deep link — whole row is the target (44px min touch height) */
  .wfoot { display: flex; align-items: center; justify-content: flex-end; gap: 6px; min-height: 44px;
    padding: 9px 15px; border-top: 1px solid var(--line); color: var(--accent);
    font-size: 0.71875rem; font-weight: calc(600 + (var(--ui-weight) - 400)); text-decoration: none; }
  .wfoot:hover { background: var(--panel-hi); }
  .wfoot .arr { transition: transform 0.13s ease; }
  .wfoot:hover .arr { transform: translateX(2px); }

  /* ---- edit mode (brief §3.4) ---- */
  .wframe.editing { outline: 1.5px dashed var(--line-2); outline-offset: 3px; }
  .wframe.grabbed { outline-color: var(--accent-line); }
  .wframe.editing .ch { cursor: grab; user-select: none; }
  /* live widgets keep rendering, but a drag never triggers their links/buttons */
  .wframe.editing .cb, .wframe.editing .wfoot { pointer-events: none; }

  .whandle { display: flex; align-items: center; justify-content: center; width: 22px; height: 26px;
    margin-left: -4px; padding: 0; background: transparent; border: 0; border-radius: 6px;
    color: var(--text-3); cursor: grab; touch-action: none; }
  .whandle[aria-pressed='true'] { color: var(--accent); }
  .whandle svg { width: 14px; height: 14px; fill: currentColor; stroke: none; }

  .wctls { display: flex; align-items: center; gap: 5px; margin-left: auto; }
  .wctl { display: flex; align-items: center; justify-content: center; width: 26px; height: 26px;
    background: var(--panel); border: 1px solid var(--line); border-radius: 7px; cursor: pointer; }
  .wctl:hover { border-color: var(--line-2); background: var(--panel-hi); }
  .wctl svg { width: 14px; height: 14px; stroke: var(--text-2); fill: none; stroke-width: 1.7; }

  /* in edit mode the controls own the right edge; meta tucks in beside the title
     (app.css gives .meta margin-left:auto, which would fight the controls for the free space) */
  .wframe.editing .ch :global(.meta) { margin-left: 0; }
</style>
