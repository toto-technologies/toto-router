<script>
  // W7 · Benchmarks (brief §4 W7) — ranked rows for one category (quiet select in the header meta),
  // score bars, one quietly emphasized leader (no podium kitsch), and the honest catalog tie-in
  // sentence. Ignores the page range: benchmark evidence isn't windowed.
  import WidgetFrame from '../WidgetFrame.svelte';
  import Skeleton from '$lib/components/Skeleton.svelte';
  import { revealIn } from '$lib/motion.js';
  import { base } from '$app/paths';
  import { query } from '$lib/api/resource.svelte.js';
  import { getBenchmarkModels } from '$lib/api/benchmarks.js';
  import { catLabel, providerHue } from '$lib/benchmarks.js';
  import { logoFor } from '$lib/logos.js';
  import { benchRows, benchSentence, providerMark } from '../org.js';

  let { range = '24h', size = 'sm' } = $props();

  let cat = $state('coding');
  const limit = $derived(size === 'lg' ? 5 : 3);
  const bq = query(() => getBenchmarkModels({ category: cat, limit }), {
    immediate: false,
    isEmpty: (d) => !d?.models?.length
  });
  $effect(() => {
    void cat;
    void limit;
    bq.reload();
  });

  // category options come from the server response; until it answers, the current pick stands alone
  const cats = $derived(bq.data?.categories?.length ? bq.data.categories : [cat]);
  const rows = $derived(benchRows(bq.data?.models ?? [], cat, limit));
  const tieIn = $derived(benchSentence(rows, cat));
</script>

<WidgetFrame id="benchmarks" title="Benchmark standouts" href="/benchmarks" linkLabel="See all benchmarks">
  {#snippet metaSnippet()}
    <select class="meta catsel" bind:value={cat} aria-label="Benchmark category">
      {#each cats as c (c)}<option value={c}>{catLabel(c)}</option>{/each}
    </select>
  {/snippet}

  {#if bq.status === 'loading'}
    <div class="sk-stack">
      {#each Array(limit + 1) as _, i}<Skeleton width={i === limit ? '65%' : '100%'} height="12px" />{/each}
    </div>
  {:else if bq.status === 'unauthed'}
    <div class="deadend"><p>Sign in to see benchmark evidence.</p></div>
  {:else if bq.status === 'forbidden'}
    <div class="deadend"><p>Benchmarks need an organization sign-in.</p></div>
  {:else if bq.status === 'error'}
    <div class="deadend">
      <p>Couldn't load benchmarks — {bq.error?.message ?? 'unknown error'}.</p>
      <button class="btn small" onclick={() => bq.reload()}>Retry</button>
    </div>
  {:else if bq.status === 'empty' || !rows.length}
    <div class="deadend"><p>No benchmark evidence for this category yet.</p></div>
  {:else}
    <div in:revealIn>
      <div class="rows">
        {#each rows as r, i (r.id)}
          <div class="brow" class:lead={r.rank === 1} style="--ph:{providerHue(r.provider)}">
            <span class="rk num">{r.rank}</span>
            <span class="pmark" aria-hidden="true">
              {#if logoFor(r.provider)}{@html logoFor(r.provider)}{:else}{providerMark(r.provider ?? r.label)}{/if}
            </span>
            <span class="modelid">{r.label}</span>
            <span class="scorebar" aria-hidden="true"><i style="--w:{(r.score ?? 0) / 100};animation-delay:{i * 40}ms"></i></span>
            <span class="num score">{r.score ?? '—'}</span>
          </div>
        {/each}
      </div>
      {#if tieIn}
        <p class="tiein">
          {tieIn.text}
          {#if tieIn.addLink}<a href="{base}/catalog">Add it →</a>{/if}
        </p>
      {/if}
    </div>
  {/if}
</WidgetFrame>

<style>
  .sk-stack { display: flex; flex-direction: column; gap: 10px; }
  .deadend { display: flex; flex-direction: column; align-items: flex-start; gap: 7px; padding: 4px 2px; }
  .deadend p { margin: 0; font-size: 0.78125rem; color: var(--text-2); }

  .catsel { appearance: none; background: transparent; border: 0; padding: 0; cursor: pointer;
    font-size: 0.71875rem; color: var(--text-2); font-family: inherit; }
  .catsel:hover { color: var(--text); }

  .rows { display: flex; flex-direction: column; gap: 3px; }
  .brow { display: flex; align-items: center; gap: 9px; padding: 5px 6px; border-radius: 8px; min-width: 0; }
  .brow.lead { background: var(--panel-2); }
  .brow.lead .score { color: var(--accent); }
  .rk { width: 14px; text-align: right; font-size: 0.71875rem; color: var(--text-3); flex: 0 0 auto; }
  /* provider identity tile — the benchmarks page's hue recipe; residency isn't known here,
     so the residency tints (teal/purple) would be a false claim */
  .pmark { width: 24px; height: 24px; border-radius: 7px; flex: 0 0 auto; display: inline-flex;
    align-items: center; justify-content: center; font-family: var(--mono); font-size: 0.625rem;
    font-weight: calc(650 + (var(--ui-weight) - 400));
    color: light-dark(hsl(var(--ph) 58% 30%), hsl(var(--ph) 52% 74%));
    background: light-dark(hsl(var(--ph) 55% 52% / 0.13), hsl(var(--ph) 50% 62% / 0.16));
    border: 1px solid light-dark(hsl(var(--ph) 48% 44% / 0.4), hsl(var(--ph) 52% 66% / 0.36)); }
  .pmark :global(svg.plogo) { width: 13px; height: 13px; fill: currentColor; }
  .brow .modelid { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .scorebar { width: 84px; height: 4px; border-radius: 3px; background: var(--line); overflow: hidden; flex: 0 0 auto; }
  .scorebar i { display: block; height: 100%; border-radius: 3px;
    background: linear-gradient(90deg, var(--accent), var(--accent-2));
    transform: scaleX(var(--w)); transform-origin: left;
    animation: growx 0.3s cubic-bezier(0.33, 1, 0.68, 1) backwards; }
  @keyframes growx { from { transform: scaleX(0); } }
  @media (prefers-reduced-motion: reduce) { .scorebar i { animation: none; } }
  .score { min-width: 24px; text-align: right; font-size: 0.75rem; }

  .tiein { margin: 10px 0 0; font-size: 0.75rem; color: var(--text-2); }
  .tiein a { color: var(--accent); font-weight: calc(600 + (var(--ui-weight) - 400)); text-decoration: none; }
  .tiein a:hover { text-decoration: underline; }
</style>
