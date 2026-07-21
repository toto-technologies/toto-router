<script>
  // W2 · Request Volume (brief §4 W2). Stacked residency bars with the MANDATORY color
  // correction: cloud = --cloud (purple), in-perimeter = --perimeter (teal). Never accent.
  import WidgetFrame from '../WidgetFrame.svelte';
  import Skeleton from '$lib/components/Skeleton.svelte';
  import { query } from '$lib/api/resource.svelte.js';
  import { getUsage } from '$lib/api/admin.js';
  import { revealIn } from '$lib/motion.js';
  import { rangeLabel } from '../registry.js';
  import { rangeWindow, foldBuckets, fillBuckets } from '../telemetry.js';

  let { range = '24h', size = 'sm' } = $props();

  const volume = query(() => {
    const w = rangeWindow(range);
    return getUsage({ groupBy: ['residency'], granularity: w.granularity, start: w.start });
  }, { immediate: false });
  $effect(() => {
    range;
    volume.reload();
  });

  const buckets = $derived(fillBuckets(foldBuckets(volume.data?.rows ?? []), range));
  const maxTot = $derived(Math.max(1, ...buckets.map((b) => b.cloud + b.local)));
  const total = $derived(buckets.reduce((n, b) => n + b.requests, 0));
  const perimPct = $derived(total > 0 ? Math.round((buckets.reduce((n, b) => n + b.local, 0) / total) * 100) : 0);

  // x-axis: hour buckets end "…THH", day buckets end "…-DD". ~6 evenly-spaced ticks, 10 at lg.
  const xticks = $derived.by(() => {
    const n = buckets.length;
    if (!n) return [];
    const want = Math.min(size === 'lg' ? 10 : 6, n);
    return Array.from({ length: want }, (_, i) => {
      const s = buckets[Math.round((i * (n - 1)) / (want - 1 || 1))].k;
      return range === '24h' ? s.slice(11, 13) : s.slice(5, 10);
    });
  });

  const meta = $derived(
    size === 'lg' && volume.status === 'ok'
      ? `${rangeLabel(range)} · ${total.toLocaleString('en-US')} requests`
      : rangeLabel(range)
  );
</script>

<WidgetFrame id="volume" title="Request volume" {meta} href="/activity" linkLabel="See all activity">
  {#if volume.status === 'loading'}
    <div class="sk-stack">{#each [100, 100, 70] as w}<Skeleton width="{w}%" height="11px" />{/each}</div>
  {:else if volume.status === 'unauthed' || volume.status === 'forbidden'}
    <div class="deadend"><p>Request volume is visible to admins and owners.</p></div>
  {:else if volume.status === 'error'}
    <div class="deadend"><p>Couldn't load request volume — {volume.error?.message ?? 'unknown error'}.</p><button class="btn small" onclick={() => volume.reload()}>Retry</button></div>
  {:else if volume.status === 'empty' || !buckets.length}
    <div class="deadend"><p>No requests in this window yet. Traffic appears here as soon as the gateway serves its first call.</p></div>
  {:else}
    <div in:revealIn>
      <div class="barchart">
        <div class="gridlines"><span></span><span></span><span></span><span></span></div>
        {#each buckets as b}
          <div class="col" title="{(b.cloud + b.local).toLocaleString('en-US')} req · {b.k}">
            <div class="bar cloudseg" style="height:{(b.cloud / maxTot) * 100}%"></div>
            <div class="bar perimseg" style="height:{(b.local / maxTot) * 100}%"></div>
          </div>
        {/each}
      </div>
      <div class="chartx">{#each xticks as t}<span>{t}</span>{/each}</div>
      <div class="legend" style="margin-top:12px">
        <span class="chip cloud"><span class="d"></span>cloud</span>
        <span class="chip perim"><span class="d"></span>in-perimeter</span>
      </div>
      <p class="summary">
        {#if perimPct >= 50}
          Most traffic in the {rangeLabel(range)} stayed in-perimeter (<span class="num">{perimPct}%</span>).
        {:else}
          Most traffic in the {rangeLabel(range)} went to cloud models (<span class="num">{100 - perimPct}%</span>).
        {/if}
      </p>
    </div>
  {/if}
</WidgetFrame>

<style>
  /* residency color rule (brief §6.1) — overrides the global .barchart accent recipe */
  .bar.cloudseg { background: linear-gradient(180deg, var(--cloud), color-mix(in srgb, var(--cloud) 70%, black)); }
  .bar.perimseg { background: linear-gradient(180deg, var(--perimeter), color-mix(in srgb, var(--perimeter) 70%, black)); }

  .summary { margin: 10px 0 0; font-size: 0.75rem; color: var(--text-2); }

  .sk-stack { display: flex; flex-direction: column; gap: 10px; }
  .deadend { display: flex; flex-direction: column; align-items: flex-start; gap: 7px; padding: 4px 2px; }
  .deadend p { margin: 0; font-size: 0.8125rem; color: var(--text-2); }
</style>
