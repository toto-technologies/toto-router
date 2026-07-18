<script>
  // Usage & Billing (C4 metering). Reproduces contenders/a-forest → Usage & Billing:
  // group-by × metric controls, a stacked spend chart, a breakdown table (mono tabular figures +
  // savings-vs-frontier + `~` estimate markers), and the Stripe-shaped export payload reveal.
  //
  // Two rollups: `chart` is time-bucketed (granularity) for the stacked bars; `table` is un-bucketed
  // totals for the breakdown. `metric` is a client-side view toggle (every row already carries all
  // three metrics) so switching it never refetches — only the group-by / date / granularity inputs do.
  import { query } from '$lib/api/resource.svelte.js';
  import { getUsage, exportUsage } from '$lib/api/admin.js';
  import SkeletonTable from '$lib/components/SkeletonTable.svelte';
  import SkeletonCard from '$lib/components/SkeletonCard.svelte';
  import SegmentedControl from '$lib/components/SegmentedControl.svelte';
  import Modal from '$lib/components/Modal.svelte';
  import { revealIn } from '$lib/motion.js';
  import { METRICS, toStacks, toBreakdown, fmtUsd, fmtCompact } from '$lib/usage.js';

  const H = 210; // chart height, px (matches .stack in app.css)

  // ---- controls ----
  const GROUPS = [
    { value: 'team', label: 'By team' },
    { value: 'model', label: 'By model' },
    { value: 'provider', label: 'By provider' },
    { value: 'label', label: 'By task' },
    { value: 'user', label: 'By user' },
  ];
  const METRIC_OPTS = [
    { value: 'cost', label: 'Cost' },
    { value: 'tokens', label: 'Tokens' },
    { value: 'requests', label: 'Requests' },
  ];
  const GRAN_OPTS = [
    { value: 'day', label: 'Day' },
    { value: 'hour', label: 'Hour' },
  ];

  const pad = (n) => String(n).padStart(2, '0');
  const firstOf = (y, m) => `${y}-${pad(m)}-01`; // m is 1-based
  const now = new Date();
  let groupBy = $state('team');
  let metric = $state('cost');
  let start = $state(firstOf(now.getFullYear(), now.getMonth() + 1));
  let end = $state(now.getMonth() === 11 ? firstOf(now.getFullYear() + 1, 1) : firstOf(now.getFullYear(), now.getMonth() + 2));
  let gran = $state('day');

  // Fraction of the selected window elapsed as of today — drives the naive month-end forecast.
  function periodFraction() {
    const s = Date.parse(start), e = Date.parse(end), t = Date.now();
    if (!(e > s)) return 1;
    return Math.max(0, Math.min(1, (Math.min(t, e) - s) / (e - s)));
  }

  // ---- data: bucketed (chart) + totals (table) ----
  const chart = query(() => getUsage({ groupBy: [groupBy], start, end, granularity: gran }), { immediate: false });
  const table = query(() => getUsage({ groupBy: [groupBy], start, end }), { immediate: false });
  // Re-query on any input that changes the SQL (metric deliberately excluded).
  $effect(() => { void groupBy; void start; void end; void gran; chart.reload(); });
  $effect(() => { void groupBy; void start; void end; table.reload(); });

  const groupLabel = $derived(GROUPS.find((g) => g.value === groupBy)?.label.replace(/^By /, '') ?? groupBy);
  const mFmt = $derived(METRICS[metric].fmt);

  const stacks = $derived(chart.data?.rows ? toStacks(chart.data.rows, groupBy, METRICS[metric].key) : null);
  const breakdown = $derived(table.data?.rows ? toBreakdown(table.data.rows, groupBy, { periodFraction: periodFraction() }) : null);

  // One page status from both queries: auth/error dead-ends come from `table`; empty only when both are.
  const status = $derived(
    table.status === 'loading' || chart.status === 'loading' ? 'loading'
    : table.status === 'unauthed' || table.status === 'forbidden' || table.status === 'error' ? table.status
    : table.status === 'empty' && chart.status === 'empty' ? 'empty'
    : 'ok',
  );

  // ---- export reveal ----
  let exportOpen = $state(false);
  let period = $state(start.slice(0, 7)); // YYYY-MM
  const ex = query(() => exportUsage({ period, format: 'stripe' }), { immediate: false });
  $effect(() => { if (exportOpen) { void period; ex.reload(); } });
  function openExport() { period = start.slice(0, 7); exportOpen = true; }
</script>

<svelte:head><title>Usage & Billing · Toto Control</title></svelte:head>

<div class="pagehead">
  <div>
    <h1>Usage &amp; Billing</h1>
    <div class="sub">Cost attribution across team · model · provider. <span class="num">~</span> marks partly-estimated rows.</div>
  </div>
  <div class="right">
    <label class="selbox" style="gap:8px">Period
      <input type="month" bind:value={period} aria-label="Billing period"
        style="background:transparent;border:0;color:inherit;font:inherit;padding:0" />
    </label>
    <button class="btn small" onclick={openExport}>Export ▾</button>
  </div>
</div>

<div style="display:flex;gap:12px;align-items:center;margin-bottom:16px;flex-wrap:wrap">
  <SegmentedControl options={GROUPS} bind:value={groupBy} accent />
  <SegmentedControl options={METRIC_OPTS} bind:value={metric} accent={false} />
  <span style="width:1px;height:22px;background:var(--line)"></span>
  <label class="selbox" style="gap:8px">From
    <input type="date" bind:value={start} aria-label="Start date"
      style="background:transparent;border:0;color:inherit;font:inherit;padding:0" />
  </label>
  <label class="selbox" style="gap:8px">To
    <input type="date" bind:value={end} aria-label="End date"
      style="background:transparent;border:0;color:inherit;font:inherit;padding:0" />
  </label>
  <SegmentedControl options={GRAN_OPTS} bind:value={gran} accent={false} />
</div>

{#if status === 'loading'}
  <div style="margin-bottom:14px"><SkeletonCard lines={5} /></div>
  <SkeletonTable rows={4} cols={6} />
{:else if status === 'unauthed'}
  <div class="stub">
    <div class="ic"><svg viewBox="0 0 24 24"><path d="M12 15v2M7 10V7a5 5 0 0 1 10 0v3" /><rect x="5" y="10" width="14" height="10" rx="2" /></svg></div>
    <b>Sign in required</b>
    <p>Your session has expired. Sign back in to view usage &amp; billing.</p>
  </div>
{:else if status === 'forbidden'}
  <div class="stub">
    <div class="ic"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /><path d="M5 5l14 14" /></svg></div>
    <b>Admin access needed</b>
    <p>Usage &amp; billing is scoped to org admins. Ask an owner to grant you the admin role.</p>
  </div>
{:else if status === 'error'}
  <div class="stub">
    <div class="ic"><svg viewBox="0 0 24 24"><path d="M12 8v5M12 16h.01" /><circle cx="12" cy="12" r="9" /></svg></div>
    <b>Couldn’t load usage</b>
    <p>{table.error?.message ?? 'Unexpected error.'}</p>
    <button class="btn small" style="margin-top:12px" onclick={() => { chart.reload(); table.reload(); }}>Retry</button>
  </div>
{:else if status === 'empty'}
  <div class="stub">
    <div class="ic"><svg viewBox="0 0 24 24"><path d="M4 19V5M4 19h16M8 16l3-4 3 2 4-6" /></svg></div>
    <b>No usage in this range</b>
    <p>Nothing metered for {start} → {end}. Widen the date range or check back after some traffic.</p>
  </div>
{:else}
  <div in:revealIn>
    <!-- Stacked spend chart -->
    <div class="card" style="margin-bottom:14px">
      <div class="ch">
        <h3>Daily {METRICS[metric].label.toLowerCase()} by {groupLabel}</h3>
        {#if stacks}
          <span class="legend" style="margin-left:auto">
            {#each stacks.series as s}
              <span class="li"><span class="sw2" style="background:{s.color}"></span>{s.name}</span>
            {/each}
          </span>
        {/if}
      </div>
      <div class="cb">
        {#if stacks && stacks.columns.length}
          <div class="stack">
            {#each stacks.columns as col}
              <div class="stackcol" title="{col.bucket} · {mFmt(col.total)}">
                {#each col.segments as seg}
                  <div class="seg2" style="height:{(seg.value / stacks.max) * H}px;background:{seg.color}"></div>
                {/each}
              </div>
            {/each}
          </div>
          <div class="stackx">
            {#each stacks.columns as col, i}
              <span>{i % 2 === 0 ? col.bucket.slice(5) : ''}</span>
            {/each}
          </div>
        {:else}
          <div class="muted" style="padding:24px 0;text-align:center">No time-series data for this window.</div>
        {/if}
      </div>
    </div>

    <!-- Breakdown table -->
    <div class="card">
      <div class="ch"><h3>Breakdown · by {groupLabel}</h3><span class="meta">{start} → {end}</span></div>
      <div class="tablewrap">
        <table>
          <thead><tr>
            <th>{groupLabel[0].toUpperCase() + groupLabel.slice(1)}</th>
            <th class="r">Requests</th><th class="r">Tokens</th><th class="r">Cost</th>
            <th class="r">Saved vs frontier</th><th class="r">Forecast (mo-end)</th>
          </tr></thead>
          <tbody>
            {#each breakdown.items as row}
              <tr>
                <td><b>{row.name}</b></td>
                <td class="r n">{fmtCompact(row.requests)}</td>
                <td class="r n">{fmtCompact(row.tokens)}</td>
                <td class="r n">{fmtUsd(row.cost)}{#if row.estimated}<span class="est">~</span>{/if}</td>
                <td class="r n saved">{fmtUsd(row.savings)}</td>
                <td class="r n" class:est={row.forecastEstimated}>{fmtUsd(row.forecast)}{#if row.forecastEstimated} ⚠{/if}</td>
              </tr>
            {/each}
          </tbody>
          <tfoot>
            <tr style="border-top:2px solid var(--line-2)">
              <td style="padding:11px 14px"><b>Org total</b></td>
              <td class="r n" style="padding:11px 14px">{fmtCompact(breakdown.total.requests)}</td>
              <td class="r n" style="padding:11px 14px">{fmtCompact(breakdown.total.tokens)}</td>
              <td class="r n" style="padding:11px 14px;color:var(--text);font-weight:calc(700 + (var(--ui-weight) - 400))">{fmtUsd(breakdown.total.cost)}</td>
              <td class="r n saved" style="padding:11px 14px">{fmtUsd(breakdown.total.savings)}</td>
              <td class="r n est" style="padding:11px 14px">{fmtUsd(breakdown.total.forecast)}</td>
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  </div>
{/if}

<!-- Export payload reveal — Stripe-shaped billing records (export SEAM, no invoice created) -->
<Modal bind:open={exportOpen} title="Export billing records" subtitle="Stripe-shaped line items · period {period}">
  {#if ex.status === 'loading'}
    <SkeletonCard lines={4} title={false} />
  {:else if ex.status === 'unauthed' || ex.status === 'forbidden'}
    <div class="notew"><svg viewBox="0 0 24 24"><path d="M12 8v5M12 16h.01" /><circle cx="12" cy="12" r="9" /></svg>
      <span>Admin access is required to export billing records.</span></div>
  {:else if ex.status === 'error'}
    <div class="notew"><svg viewBox="0 0 24 24"><path d="M12 8v5M12 16h.01" /><circle cx="12" cy="12" r="9" /></svg>
      <span>{ex.error?.message ?? 'Export failed.'}</span></div>
  {:else if ex.status === 'empty'}
    <div class="muted" style="padding:8px 0">No billable line items for {period}.</div>
  {:else if ex.data}
    <div class="field">
      <label>Line items · {ex.data.line_items.length}</label>
      <div class="tablewrap" style="border:1px solid var(--line);border-radius:8px;max-height:220px;overflow:auto">
        <table>
          <thead><tr><th>Team</th><th>Model</th><th class="r">Tokens</th><th class="r">Cost</th></tr></thead>
          <tbody>
            {#each ex.data.line_items as li}
              <tr>
                <td class="n">{li.team_id ?? '—'}</td>
                <td class="modelid">{li.model}</td>
                <td class="r n">{fmtCompact(li.quantity_tokens)}</td>
                <td class="r n">{fmtUsd(li.cost_usd)}{#if li.estimated}<span class="est">~</span>{/if}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </div>
    <div class="field" style="margin-bottom:0">
      <label>Raw payload</label>
      <pre class="num" style="margin:0;max-height:200px;overflow:auto;background:var(--panel-2);border:1px solid var(--line);border-radius:8px;padding:11px;font-size:0.6875rem;line-height:1.5">{JSON.stringify(ex.data, null, 2)}</pre>
    </div>
  {/if}
</Modal>
