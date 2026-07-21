<script>
  // W3 · Models (brief §4 W3) — top models by spend: residency-tinted logo tiles, calls + spend,
  // animated share-of-spend bars. lg adds a calls-trend delta chip + "via {provider}". Range-aware:
  // both the current and the prior equal-length window load together so the delta is always honest.
  import WidgetFrame from '../WidgetFrame.svelte';
  import Skeleton from '$lib/components/Skeleton.svelte';
  import { revealIn } from '$lib/motion.js';
  import { query } from '$lib/api/resource.svelte.js';
  import { getUsage, getCatalogModels } from '$lib/api/admin.js';
  import { logoFor } from '$lib/logos.js';
  import { providerLabel } from '$lib/models.js';
  import { topModels, providerMark, compact, usd, rangeStartISO, prevRangeStartISO } from '../org.js';

  let { range = '24h', size = 'sm' } = $props();

  const usage = query(
    () => {
      const now = Date.now();
      const start = rangeStartISO(range, now);
      return Promise.all([
        getUsage({ groupBy: ['model', 'residency'], start }),
        getUsage({ groupBy: ['model'], start: prevRangeStartISO(range, now), end: start })
      ]).then(([cur, prev]) => ({ cur, prev }));
    },
    { immediate: false, isEmpty: (d) => !d?.cur?.rows?.length }
  );
  $effect(() => {
    void range;
    usage.reload();
  });
  const catalog = query(() => getCatalogModels());

  const rows = $derived(
    topModels(usage.data?.cur?.rows, usage.data?.prev?.rows, catalog.data?.models ?? [], size === 'lg' ? 8 : 5)
  );
  // catalog is member-gated garnish (vendor/provider); usage drives the widget's states
  const loading = $derived(usage.status === 'loading' || catalog.status === 'loading');
</script>

<WidgetFrame id="models" title="Your top models" meta="by spend" href="/catalog" linkLabel="Manage catalog & routing">
  {#if loading}
    <div class="sk-stack">
      {#each Array(size === 'lg' ? 6 : 4) as _, i}<Skeleton width={i % 2 ? '82%' : '100%'} height="12px" />{/each}
    </div>
  {:else if usage.status === 'unauthed'}
    <div class="deadend"><p>Sign in to see model usage.</p></div>
  {:else if usage.status === 'forbidden'}
    <div class="deadend"><p>Model usage is org-wide — it needs admin access.</p></div>
  {:else if usage.status === 'error'}
    <div class="deadend">
      <p>Couldn't load model usage — {usage.error?.message ?? 'unknown error'}.</p>
      <button class="btn small" onclick={() => usage.reload()}>Retry</button>
    </div>
  {:else if usage.status === 'empty' || !rows.length}
    <div class="deadend"><p>No model usage yet. Models appear here once requests route through the catalog.</p></div>
  {:else}
    <div class="rows" in:revealIn>
      {#each rows as m, i (m.id)}
        <div class="mrow">
          <span
            class="mglyph {m.residency === 'cloud' ? 'cloud' : 'local'}"
            title={m.residency === 'cloud' ? 'cloud' : 'in-perimeter'}
          >
            {#if logoFor(m.vendor)}{@html logoFor(m.vendor)}{:else}<span class="mono mark">{providerMark(m.vendor ?? m.id)}</span>{/if}
          </span>
          <span class="modelid">{m.id}</span>
          {#if size === 'lg'}
            {#if m.delta}<span class="delta {m.delta.dir}">{m.delta.label}</span>{/if}
            {#if m.provider}<span class="via">via {providerLabel(m.provider)}</span>{/if}
          {/if}
          <span class="num stat">{compact(m.calls)} {m.calls === 1 ? 'call' : 'calls'}</span>
          <span class="num stat cost">{usd(m.cost)}</span>
          <span class="sharebar" aria-hidden="true"><i style="--w:{m.share / 100};animation-delay:{i * 40}ms"></i></span>
          <span class="num share">{m.share}%</span>
        </div>
      {/each}
    </div>
  {/if}
</WidgetFrame>

<style>
  .sk-stack { display: flex; flex-direction: column; gap: 10px; }
  .deadend { display: flex; flex-direction: column; align-items: flex-start; gap: 7px; padding: 4px 2px; }
  .deadend p { margin: 0; font-size: 0.78125rem; color: var(--text-2); }

  .rows { display: flex; flex-direction: column; gap: 4px; }
  .mrow { display: flex; align-items: center; gap: 9px; padding: 4px 0; min-width: 0; }
  .mrow .modelid { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  /* vendored logo marks are filled paths; app.css .mglyph svg defaults to stroke-only */
  .mglyph :global(svg.plogo) { width: 13px; height: 13px; fill: currentColor; stroke: none; }
  .mark { font-size: 0.625rem; font-weight: calc(650 + (var(--ui-weight) - 400)); }
  .stat { font-size: 0.71875rem; color: var(--text-2); white-space: nowrap; }
  .stat.cost { color: var(--text); min-width: 42px; text-align: right; }
  .share { font-size: 0.71875rem; color: var(--text-2); min-width: 32px; text-align: right; }

  /* share-of-spend bar — accent is a brand-neutral ranking here, never a residency claim (§4 W3) */
  .sharebar { width: 62px; height: 4px; border-radius: 3px; background: var(--line); overflow: hidden; flex: 0 0 auto; }
  .sharebar i { display: block; height: 100%; border-radius: 3px;
    background: linear-gradient(90deg, var(--accent), var(--accent-2));
    transform: scaleX(var(--w)); transform-origin: left;
    animation: growx 0.3s cubic-bezier(0.33, 1, 0.68, 1) backwards; }
  @keyframes growx { from { transform: scaleX(0); } }
  @media (prefers-reduced-motion: reduce) { .sharebar i { animation: none; } }
</style>
