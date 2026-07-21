<script>
  // W5 · Cache Savings (brief §4 W5). getCacheSavings → hero $ figure, getCacheHealth →
  // token-weighted hit rate + the animated .wbar meter. sm-only widget.
  import WidgetFrame from '../WidgetFrame.svelte';
  import Skeleton from '$lib/components/Skeleton.svelte';
  import { query } from '$lib/api/resource.svelte.js';
  import { getCacheSavings, getCacheHealth } from '$lib/api/admin.js';
  import { revealIn } from '$lib/motion.js';
  import { rangeLabel } from '../registry.js';
  import { rangeWindow, usd, overallHitRate } from '../telemetry.js';

  let { range = '24h', size = 'sm' } = $props();

  const savings = query(() => {
    const w = rangeWindow(range);
    return getCacheSavings({ from: w.fromS, to: w.toS });
  }, {
    immediate: false,
    isEmpty: (d) => !d?.total || (!d.total.tokens_cached && !d.total.net_usd)
  });
  const health = query(() => {
    const w = rangeWindow(range);
    return getCacheHealth({ from: w.fromS, to: w.toS, granularity: w.granularity });
  }, { immediate: false, isEmpty: (d) => !d?.buckets?.length });

  $effect(() => {
    range;
    savings.reload();
    health.reload();
  });

  const hitPct = $derived(Math.round(overallHitRate(health.data?.buckets ?? []) * 100));
  const loading = $derived(savings.status === 'loading' || health.status === 'loading');
</script>

<WidgetFrame id="cache" title="Cache savings" meta={rangeLabel(range)} href="/cache" linkLabel="See cache details">
  {#if loading}
    <div class="sk-stack">{#each [60, 100, 70] as w}<Skeleton width="{w}%" height="11px" />{/each}</div>
  {:else if savings.status === 'unauthed' || savings.status === 'forbidden'}
    <div class="deadend"><p>Cache savings are visible to admins and owners.</p></div>
  {:else if savings.status === 'error'}
    <div class="deadend"><p>Couldn't load cache savings — {savings.error?.message ?? 'unknown error'}.</p><button class="btn small" onclick={() => { savings.reload(); health.reload(); }}>Retry</button></div>
  {:else if savings.status === 'empty'}
    <div class="deadend"><p>Nothing cached this period yet. Caching kicks in automatically on repeated prompts — no setup needed.</p></div>
  {:else}
    <div in:revealIn>
      <div class="hero"><span class="big num">{usd(savings.data.total.net_usd)}</span><span class="cap">saved by caching</span></div>
      {#if health.status === 'ok'}
        <p class="sent"><span class="num">{hitPct}%</span> of eligible requests hit the cache.</p>
        <div class="meterrow">
          <span class="eyebrow">hit rate</span>
          <div class="wbar"><i class="fill" style="width:{hitPct}%"></i></div>
          <span class="num pct">{hitPct}%</span>
        </div>
      {/if}
    </div>
  {/if}
</WidgetFrame>

<style>
  .hero { display: flex; align-items: baseline; gap: 8px; }
  .hero .big { font-family: var(--mono); font-size: 1.5625rem; font-weight: calc(600 + (var(--ui-weight) - 400)); letter-spacing: -0.02em; line-height: 1; color: var(--good); }
  .hero .cap { font-size: 0.8125rem; color: var(--text-2); }

  .sent { margin: 10px 0 0; font-size: 0.75rem; color: var(--text-2); }

  .meterrow { display: flex; align-items: center; gap: 9px; margin-top: 11px; }
  .meterrow .wbar { flex: 1; }
  .meterrow .pct { font-size: 0.75rem; color: var(--text-2); }
  .fill { background: linear-gradient(90deg, var(--good), color-mix(in srgb, var(--good) 60%, var(--accent-2)));
    transform-origin: left; animation: grow 0.3s cubic-bezier(0.33, 1, 0.68, 1); }
  /* §5 meter reveal — the global prefers-reduced-motion rule zeroes this animation */
  @keyframes grow { from { transform: scaleX(0); } }

  .sk-stack { display: flex; flex-direction: column; gap: 10px; }
  .deadend { display: flex; flex-direction: column; align-items: flex-start; gap: 7px; padding: 4px 2px; }
  .deadend p { margin: 0; font-size: 0.8125rem; color: var(--text-2); }
</style>
