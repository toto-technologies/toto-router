<script>
  // W1 · Spend & Usage KPIs (brief §4 W1). Three usage slices: models (period totals),
  // residency×bucket (sparklines), previous equal-length window (delta chips).
  import WidgetFrame from '../WidgetFrame.svelte';
  import Skeleton from '$lib/components/Skeleton.svelte';
  import { query } from '$lib/api/resource.svelte.js';
  import { getUsage } from '$lib/api/admin.js';
  import { revealIn } from '$lib/motion.js';
  import { rangeLabel } from '../registry.js';
  import { rangeWindow, prevLabel, usd, count, usageTotals, foldBuckets, fillBuckets, deltaPct, spark } from '../telemetry.js';

  let { range = '24h', size = 'lg' } = $props();

  const models = query(() => getUsage({ groupBy: ['model', 'residency'], start: rangeWindow(range).start }), { immediate: false });
  const volume = query(() => {
    const w = rangeWindow(range);
    return getUsage({ groupBy: ['residency'], granularity: w.granularity, start: w.start });
  }, { immediate: false });
  const prev = query(() => {
    const w = rangeWindow(range);
    return getUsage({ groupBy: ['residency'], start: w.prevStart, end: w.prevEnd });
  }, { immediate: false });

  // first load + refetch whenever the page range changes
  $effect(() => {
    range;
    models.reload();
    volume.reload();
    prev.reload();
  });
  const retry = () => { models.reload(); volume.reload(); prev.reload(); };

  const totals = $derived(usageTotals(models.data?.rows ?? []));
  const prevTotals = $derived(usageTotals(prev.data?.rows ?? []));
  const buckets = $derived(fillBuckets(foldBuckets(volume.data?.rows ?? []), range));

  const savingsPct = $derived.by(() => {
    const base = totals.cost + totals.savings;
    return base > 0 ? Math.round((totals.savings / base) * 100) : 0;
  });

  const dSpent = $derived(deltaPct(totals.cost, prevTotals.cost));
  const dSaved = $derived(deltaPct(totals.savings, prevTotals.savings));
  const dReq = $derived(deltaPct(totals.requests, prevTotals.requests));
  const dTok = $derived(deltaPct(totals.tokens, prevTotals.tokens));

  const sReq = $derived(spark(buckets.map((b) => b.requests)));
  const sTok = $derived(spark(buckets.map((b) => b.tokens)));
  const sSav = $derived(spark(buckets.map((b) => b.savings)));

  // A sparkline earns its pixels only when there's a shape to show — at least two
  // nonzero buckets. A lone busy bucket renders as the same right angle on every card,
  // which reads as decoration, not data.
  const trendable = (vals) => vals.filter((v) => v > 0).length >= 2;
  const tReq = $derived(trendable(buckets.map((b) => b.requests)));
  const tTok = $derived(trendable(buckets.map((b) => b.tokens)));
  const tSav = $derived(trendable(buckets.map((b) => b.savings)));

  const trend = (pct) =>
    pct == null ? 'no earlier traffic to compare'
    : pct > 0 ? `up ${pct}% on ${prevLabel(range)}`
    : pct < 0 ? `down ${-pct}% on ${prevLabel(range)}`
    : `level with ${prevLabel(range)}`;

  const loading = $derived(models.status === 'loading' || volume.status === 'loading' || prev.status === 'loading');
</script>

{#snippet chip(pct, neutral = false)}
  {#if pct != null}
    <span class="delta {neutral ? 'flat' : pct > 0 ? 'up' : pct < 0 ? 'down' : 'flat'}">{pct > 0 ? '▲' : pct < 0 ? '▼' : ''}{Math.abs(pct)}%</span>
  {/if}
{/snippet}

<WidgetFrame id="spend" title="How you're doing" meta={rangeLabel(range)} href="/usage" linkLabel="See usage & billing">
  {#if loading}
    <div class="wkpis" class:sm={size === 'sm'}>
      {#each Array(4) as _}<div class="kpi"><Skeleton width="55%" height="10px" /><div style="margin-top:10px"><Skeleton width="70%" height="22px" /></div></div>{/each}
    </div>
  {:else if models.status === 'unauthed' || models.status === 'forbidden'}
    <div class="deadend"><p>Org-wide spend is visible to admins and owners.</p></div>
  {:else if models.status === 'error'}
    <div class="deadend"><p>Couldn't load usage — {models.error?.message ?? 'unknown error'}.</p><button class="btn small" onclick={retry}>Retry</button></div>
  {:else if models.status === 'empty'}
    <div class="deadend"><p>No traffic yet this period. Once requests flow, your spend and savings show up here.</p></div>
  {:else}
    {@const req = count(totals.requests)}
    {@const tok = count(totals.tokens)}
    <div class="wkpis" class:sm={size === 'sm'} in:revealIn>
      <div class="kpi">
        <div class="lab">Spent</div>
        <div class="big num bigrow">{usd(totals.cost)}{@render chip(dSpent, true)}</div>
        <div class="sub">{trend(dSpent)}</div>
      </div>
      <div class="kpi">
        <div class="lab">Saved by routing</div>
        {#if tSav}
          <svg class="spark" viewBox="0 0 78 30" aria-hidden="true">
            <path d={sSav.area} fill="var(--good)" opacity="0.10" />
            <path d={sSav.line} fill="none" stroke="var(--good)" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" />
            <circle cx={sSav.last[0].toFixed(1)} cy={sSav.last[1].toFixed(1)} r="2.1" fill="var(--good)" />
          </svg>
        {/if}
        <div class="big num bigrow saved">{usd(totals.savings)}{@render chip(dSaved)}</div>
        <div class="sub">
          {#if totals.savings > 0 && savingsPct > 0}
            <span class="cheaper">{savingsPct}% cheaper</span>&nbsp;than frontier-only
          {:else}
            {trend(dSaved)}
          {/if}
        </div>
      </div>
      <div class="kpi">
        <div class="lab">Requests</div>
        {#if tReq}
          <svg class="spark" viewBox="0 0 78 30" aria-hidden="true">
            <path d={sReq.area} fill="var(--accent)" opacity="0.10" />
            <path d={sReq.line} fill="none" stroke="var(--accent)" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" />
            <circle cx={sReq.last[0].toFixed(1)} cy={sReq.last[1].toFixed(1)} r="2.1" fill="var(--accent)" />
          </svg>
        {/if}
        <div class="big num bigrow">{req.big}<small>{req.unit}</small>{@render chip(dReq)}</div>
        <div class="sub">{trend(dReq)}</div>
      </div>
      <div class="kpi">
        <div class="lab">Tokens</div>
        {#if tTok}
          <svg class="spark" viewBox="0 0 78 30" aria-hidden="true">
            <path d={sTok.area} fill="var(--accent)" opacity="0.10" />
            <path d={sTok.line} fill="none" stroke="var(--accent)" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" />
            <circle cx={sTok.last[0].toFixed(1)} cy={sTok.last[1].toFixed(1)} r="2.1" fill="var(--accent)" />
          </svg>
        {/if}
        <div class="big num bigrow">{tok.big}<small>{tok.unit}</small>{@render chip(dTok)}</div>
        <div class="sub">prompt + completion</div>
      </div>
    </div>
  {/if}
</WidgetFrame>

<style>
  .wkpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
  /* sm size (brief W1): 2×2 tile grid, sparks hidden, delta chips kept */
  .wkpis.sm { grid-template-columns: repeat(2, 1fr); }
  .wkpis.sm .spark { display: none; }
  @media (max-width: 900px) { .wkpis { grid-template-columns: repeat(2, 1fr); } }

  .bigrow { display: flex; align-items: baseline; gap: 7px; }
  .saved { color: var(--good); }
  .cheaper { color: var(--good); font-weight: calc(600 + (var(--ui-weight) - 400)); }

  .deadend { display: flex; flex-direction: column; align-items: flex-start; gap: 7px; padding: 4px 2px; }
  .deadend p { margin: 0; font-size: 0.78125rem; color: var(--text-2); }
</style>
