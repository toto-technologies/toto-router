<script>
  // W4 · Provider Health (brief §4 W4). Live getProviderHealth() polled every 30s; breaker
  // states in plain words, retry countdown ticks locally between polls, failover sentence.
  import WidgetFrame from '../WidgetFrame.svelte';
  import Skeleton from '$lib/components/Skeleton.svelte';
  import { query } from '$lib/api/resource.svelte.js';
  import { getProviderHealth } from '$lib/api/admin.js';
  import { revealIn } from '$lib/motion.js';
  import { providerState, providerDisplay, healthSummary, fmtMs } from '../telemetry.js';

  let { range = '24h', size = 'sm' } = $props();

  let fetchedAt = $state(0);
  let now = $state(Date.now());

  const health = query(
    () => getProviderHealth({}).then((d) => ((fetchedAt = Date.now()), d)),
    { isEmpty: (d) => !d?.providers?.length }
  );

  // poll while mounted; a 1s local tick drives the retry countdown + freshness meta
  $effect(() => {
    const poll = setInterval(() => health.reload(), 30_000);
    const tick = setInterval(() => (now = Date.now()), 1_000);
    return () => {
      clearInterval(poll);
      clearInterval(tick);
    };
  });

  const providers = $derived(health.data?.providers ?? []);
  const fresh = $derived(fetchedAt > 0 && now - fetchedAt < 30_000);
  const agoMin = $derived(Math.max(1, Math.round((now - fetchedAt) / 60_000)));

  // seconds left before the breaker's half-open trial, counted down locally between polls
  const retryLeft = (p) =>
    p.retry_in == null ? null : Math.max(0, Math.round(p.retry_in - (now - fetchedAt) / 1000));
</script>

<WidgetFrame id="health" title="Provider health" href="/analytics" linkLabel="See routing status">
  {#snippet metaSnippet()}
    {#if fetchedAt > 0}
      {#if fresh}<span class="meta liverow"><span class="livedot"></span>live</span>
      {:else}<span class="meta">as of {agoMin}m ago</span>{/if}
    {/if}
  {/snippet}
  {#if health.status === 'loading'}
    <div class="sk-stack">{#each [100, 100, 70] as w}<Skeleton width="{w}%" height="11px" />{/each}</div>
  {:else if health.status === 'unauthed' || health.status === 'forbidden'}
    <div class="deadend"><p>Provider health is visible to admins and owners.</p></div>
  {:else if health.status === 'error'}
    <div class="deadend"><p>Couldn't load provider health — {health.error?.message ?? 'unknown error'}.</p><button class="btn small" onclick={() => health.reload()}>Retry</button></div>
  {:else if health.status === 'empty'}
    <div class="deadend"><p>No providers configured yet. Add one in Catalog & Routing.</p></div>
  {:else}
    <div in:revealIn>
      <div class="health hgrid" class:two={size === 'sm'}>
        {#each providers as p}
          {@const st = providerState(p.state)}
          <div class="p">
            <div class="nm" title={p.provider}>{providerDisplay(p.provider)}</div>
            <div class="st">
              <span class="state {st.cls}" title="circuit breaker: {p.state}"><span class="d"></span>{st.word}</span>
            </div>
            <div class="lat">
              {#if p.state === 'open' && retryLeft(p) != null}retry in {retryLeft(p)}s{:else}p95 {fmtMs(p.stats?.latency_p95_ms)}{/if}
            </div>
          </div>
        {/each}
      </div>
      <p class="summary">{healthSummary(providers)}</p>
    </div>
  {/if}
</WidgetFrame>

<style>
  /* sm wraps 2×2, lg stays 4-across (global .health); hairlines follow the wrap */
  .hgrid.two { grid-template-columns: repeat(2, 1fr); }
  .hgrid.two .p:nth-child(2n) { border-right: 0; }
  .hgrid.two .p:nth-child(n + 3) { border-top: 1px solid var(--line); }
  .hgrid:not(.two) .p:nth-child(4n) { border-right: 0; }
  .hgrid:not(.two) .p:nth-child(n + 5) { border-top: 1px solid var(--line); }
  .hgrid .p:first-child { padding-left: 2px; }

  .summary { margin: 12px 0 0; padding: 0 2px; font-size: 0.75rem; color: var(--text-2); }

  .liverow { display: inline-flex; align-items: center; gap: 6px; }
  .livedot { width: 6px; height: 6px; border-radius: 50%; background: var(--good); box-shadow: 0 0 8px 0 var(--good); }

  .sk-stack { display: flex; flex-direction: column; gap: 10px; }
  .deadend { display: flex; flex-direction: column; align-items: flex-start; gap: 7px; padding: 4px 2px; }
  .deadend p { margin: 0; font-size: 0.78125rem; color: var(--text-2); }
</style>
