<script>
  import Table from '$lib/components/Table.svelte';
  import Chip from '$lib/components/Chip.svelte';
  import Modal from '$lib/components/Modal.svelte';
  import SegmentedControl from '$lib/components/SegmentedControl.svelte';
  import SkeletonTable from '$lib/components/SkeletonTable.svelte';
  import { query } from '$lib/api/resource.svelte.js';
  import { getRequests, getRequestDetail, listMembers, getMe } from '$lib/api/admin.js';
  import { prettyModel, priceFmt } from '$lib/models.js';
  import { taskLabel } from '$lib/tasks.js';
  import { fmtTime, relTime, isoAttr } from '$lib/time.js';
  import { revealIn } from '$lib/motion.js';

  // Inlined edition check (not $lib/edition.js) so it folds at build time — vite.config.js `define`.
  // OSS is single-tenant: no members endpoint, so activity is always the caller's own requests.
  const OSS = typeof __EDITION__ !== 'undefined' && __EDITION__ === 'oss';

  const LIMIT = 50;
  const WINDOWS = [
    { value: '24h', label: '24h', secs: 86400 },
    { value: '7d', label: '7d', secs: 7 * 86400 },
    { value: '30d', label: '30d', secs: 30 * 86400 }
  ];

  let win = $state('24h');
  let model = $state('');
  let label = $state('');
  let offset = $state(0);
  let selected = $state(null); // the row whose drill-down is open
  let detailOpen = $state(false);

  // Scope header: derive role honestly. listMembers 403s for a plain member (data stays null) → they
  // only ever see their own requests server-side, so "Your requests" is correct; an admin/owner who
  // can list members and finds their own row as admin/owner sees the org. No role signal = "Requests".
  const me = query(() => getMe());
  // listMembers (/v1/admin/members) is enterprise-only; OSS skips it and the scope stays personal.
  const members = query(() => listMembers(), { immediate: !OSS });
  const myRole = $derived(
    (members.data?.members ?? []).find((m) => m.user_id === me.data?.user_id)?.role
  );
  const scopeLabel = $derived(
    myRole === 'admin' || myRole === 'owner'
      ? 'Organization requests'
      : me.data
        ? 'Your requests'
        : 'Requests'
  );

  // Model filter options = models actually seen in this traffic (accumulated across fetches),
  // not the whole catalog — every option is guaranteed to match at least one logged request.
  let modelOpts = $state([]);
  $effect(() => {
    const seen = new Set(modelOpts);
    for (const r of reqs.data?.requests ?? []) if (r.model) seen.add(r.model);
    if (seen.size !== modelOpts.length) modelOpts = [...seen].sort();
  });

  function windowRange() {
    const now = Math.floor(Date.now() / 1000);
    const w = WINDOWS.find((x) => x.value === win) ?? WINDOWS[0];
    return { from: now - w.secs, to: now };
  }

  const reqs = query(() => {
    const { from, to } = windowRange();
    return getRequests({
      from,
      to,
      model: model || undefined,
      label: label || undefined,
      limit: LIMIT,
      offset
    });
  });

  const rows = $derived(reqs.data?.requests ?? []);
  const hasNext = $derived(reqs.data?.next_offset != null);

  function refetch() {
    offset = 0;
    reqs.reload();
  }
  function nextPage() {
    if (reqs.data?.next_offset != null) offset = reqs.data.next_offset;
    reqs.reload();
  }
  function prevPage() {
    offset = Math.max(0, offset - LIMIT);
    reqs.reload();
  }
  // Content detail is fetched on demand when a row opens (deferred; content can be long/absent).
  // isEmpty:false — the {request, content_available, prompt?, response?} shape is never "empty".
  const detail = query(() => getRequestDetail(selected.id), {
    immediate: false,
    isEmpty: () => false
  });
  function openRow(r) {
    selected = r;
    detailOpen = true;
    if (r.id != null) detail.reload();
  }

  const usd = (n) => (n == null ? '—' : '$' + priceFmt(n));

  // One-sentence "why this model", derived from route_reason (colon-segmented, e.g.
  // label:code_generation:team, smart:classify_failed). Falls back to a literal echo.
  // Scope tokens (:team) are internal vocabulary — this single-tenant console says
  // "your routing policy"; the raw reason stays visible in the detail rows below.
  function explainReason(reason, served) {
    const m = prettyModel(served);
    if (!reason) return `Served by ${m}.`;
    const parts = reason.split(':');
    const kind = parts[0];
    const scope = parts[parts.length - 1];
    if (kind === 'label') {
      const task = taskLabel(parts[1]) || 'this task';
      if (scope === 'warm-hold')
        return `Classified as “${task}”, but the session was held on ${m} — its prompt cache is still warm, so switching would cost more than it saves.`;
      const via =
        scope === 'global'
          ? 'the global default policy'
          : scope === 'fallback'
            ? 'a fallback (no policy matched)'
            : 'your routing policy';
      return `Classified as “${task}” and routed to ${m} by ${via}.`;
    }
    if (kind === 'smart') {
      if (parts[1] === 'classify_failed')
        return `Classification failed, so ${m} was used as the smart-route default.`;
      return `Smart auto-route selected ${m} for this task.`;
    }
    if (kind === 'default') return `${m} served this as the default model (no task-specific rule).`;
    return `Served by ${m} (${reason}).`;
  }

  // Sub-line under the task chip — ONLY when the reason says more than the chip already does.
  // A routine policy match (label:<task>[:scope]) is silent; holds, fallbacks, cache hits, and
  // failures get plain language. Anything unrecognized echoes raw so nothing is hidden.
  function reasonExtra(reason) {
    if (!reason) return '';
    if (reason === 'cache') return 'served from cache';
    if (reason === 'catalog') return 'model requested directly';
    const parts = reason.split(':');
    if (parts[0] === 'label') {
      const scope = parts[2];
      if (scope === 'warm-hold') return 'held on session — prompt cache still warm';
      if (scope === 'fallback') return 'fallback — no policy matched';
      return '';
    }
    if (reason === 'smart:classify_failed') return 'classification failed — default model';
    if (reason === 'smart:policy_error') return 'policy error — default model';
    return reason;
  }

  // Ordered field list for the drill-down. `v` reads the value off the selected row at render time.
  const detailRows = [
    { k: 'Classified as', v: (r) => taskLabel(r.classified_as) || 'Unclassified' },
    { k: 'Model served', v: (r) => prettyModel(r.model) },
    { k: 'Route reason', v: (r) => r.route_reason || '—', mono: true },
    { k: 'Residency', v: (r) => r.residency || '—' },
    { k: 'Guard action', v: (r) => r.guard_action || 'none' },
    { k: 'Status', v: (r) => r.status || '—' },
    {
      k: 'Tokens (prompt / completion / cached)',
      v: (r) =>
        `${r.tokens_prompt ?? 0} / ${r.tokens_completion ?? 0} / ${r.tokens_cached ?? 0}`,
      mono: true
    },
    {
      k: 'Cost',
      v: (r) => usd(r.cost_usd) + (r.cost_estimated ? ' (estimated)' : ''),
      mono: true
    },
    { k: 'Frontier baseline', v: (r) => usd(r.frontier_baseline_usd), mono: true },
    {
      k: 'Saved vs frontier',
      v: (r) =>
        r.frontier_baseline_usd != null && r.cost_usd != null
          ? usd(r.frontier_baseline_usd - r.cost_usd)
          : '—',
      mono: true
    },
    { k: 'Latency', v: (r) => (r.latency_ms == null ? '—' : `${r.latency_ms} ms`), mono: true }
  ];
</script>

<svelte:head><title>Activity · Toto Control</title></svelte:head>

<div class="pagehead">
  <div>
    <h1>Activity</h1>
    <div class="sub">
      {scopeLabel} · the per-request routing decision trail.
      <span class="muted"
        >Requests log the routing decision and, when content logging is on, the prompt + response —
        visible only to you{OSS ? '' : ' and your org admins'}.</span>
    </div>
  </div>
</div>

<div class="card" style="margin-bottom:14px">
  <div class="cb" style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end">
    <div class="field" style="margin-bottom:0">
      <span class="lbl">Range</span>
      <SegmentedControl options={WINDOWS} bind:value={win} onchange={refetch} />
    </div>
    <div class="field" style="margin-bottom:0">
      <label for="f-model">Model</label>
      <select id="f-model" class="routesel" bind:value={model} onchange={refetch}>
        <option value="">All models</option>
        {#each modelOpts as id}
          <option value={id}>{prettyModel(id)}</option>
        {/each}
      </select>
    </div>
    <div class="field" style="margin-bottom:0">
      <label for="f-label">Task</label>
      <input
        id="f-label"
        placeholder="e.g. code_generation"
        bind:value={label}
        onchange={refetch} />
    </div>
    {#if model || label || win !== '24h'}
      <button
        class="btn small ghost"
        onclick={() => {
          model = '';
          label = '';
          win = '24h';
          refetch();
        }}>Clear</button>
    {/if}
  </div>
</div>

{#if reqs.status === 'loading'}
  <SkeletonTable rows={8} cols={6} />
{:else if reqs.status === 'unauthed'}
  <div class="stub"><b>Sign in required</b><p>Your session has expired. Sign in again to view activity.</p></div>
{:else if reqs.status === 'forbidden'}
  <div class="stub"><b>Not available</b><p>You don't have access to the request log for this scope.</p></div>
{:else if reqs.status === 'error'}
  <div class="stub"><b>Couldn't load activity</b><p>{reqs.error?.message || 'Something went wrong.'}</p></div>
{:else}
  <div in:revealIn>
    <div class="card">
      <div class="ch">
        <h3>Requests</h3>
        <span class="meta">{rows.length === 0 ? 'none' : `${offset + 1}–${offset + rows.length}`}</span>
      </div>
      {#if reqs.status === 'empty' || rows.length === 0}
        <div class="cb" style="text-align:center;color:var(--text-3);padding:36px 15px">
          {model || label
            ? 'No requests match these filters.'
            : 'No requests yet — send one via the API or pi, then it appears here.'}
        </div>
      {:else}
        <Table>
          {#snippet head()}
            <tr>
              <th>Time</th><th>Task</th><th>Model</th>
              <th class="r">Tokens</th><th class="r">Cost</th><th class="r">Latency</th>
            </tr>
          {/snippet}
          {#each rows as r, i (i)}
            <tr class="clickrow" onclick={() => openRow(r)} title="View decision trail">
              <td>
                <time
                  class="mono muted"
                  datetime={isoAttr(r.ts)}
                  title={fmtTime(r.ts)}>{relTime(r.ts)}</time>
              </td>
              <td>
                {#if r.classified_as}
                  <Chip>{taskLabel(r.classified_as)}</Chip>
                {:else}
                  <span class="muted">Unclassified</span>
                {/if}
                {#if reasonExtra(r.route_reason)}
                  <span class="rx">{reasonExtra(r.route_reason)}</span>
                {/if}
              </td>
              <td class="rowlead">{prettyModel(r.model)}</td>
              <td class="r n" title="{r.tokens_prompt ?? 0} in · {r.tokens_completion ?? 0} out"
                >{r.tokens_prompt ?? 0}·{r.tokens_completion ?? 0}</td>
              <td class="r n">{usd(r.cost_usd)}{#if r.cost_estimated}<span class="est">~</span>{/if}</td>
              <td class="r n">{r.latency_ms == null ? '—' : `${r.latency_ms} ms`}</td>
            </tr>
          {/each}
        </Table>
      {/if}
    </div>
    {#if offset > 0 || hasNext}
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px">
        <button class="btn small" disabled={offset === 0} onclick={prevPage}>Prev</button>
        <button class="btn small" disabled={!hasNext} onclick={nextPage}>Next</button>
      </div>
    {/if}
  </div>
{/if}

<Modal bind:open={detailOpen} title="Request detail" subtitle={selected ? fmtTime(selected.ts) : ''}>
  {#if selected}
    <p class="why">{explainReason(selected.route_reason, selected.model)}</p>
    <dl class="detail">
      {#each detailRows as f}
        <div class="drow">
          <dt>{f.k}</dt>
          <dd class:mono={f.mono}>{f.v(selected)}</dd>
        </div>
      {/each}
    </dl>
    <div class="content">
      {#if detail.status === 'loading'}
        <p class="note">Loading content…</p>
      {:else if detail.status === 'ok' && detail.data?.content_available}
        <div class="cblock">
          <h4>Prompt</h4>
          <div class="scroll mono">
            {#each detail.data.prompt ?? [] as msg}
              <div class="msg">
                <span class="role">{msg.role}</span>
                <pre>{typeof msg.content === 'string'
                    ? msg.content
                    : JSON.stringify(msg.content, null, 2)}</pre>
              </div>
            {/each}
          </div>
        </div>
        <div class="cblock">
          <h4>Response</h4>
          <div class="scroll mono"><pre>{detail.data.response ?? ''}</pre></div>
        </div>
      {:else if detail.status === 'error' || detail.status === 'unauthed' || detail.status === 'forbidden'}
        <p class="note">Couldn't load content — {detail.error?.message || 'try again.'}</p>
      {:else}
        <p class="note">
          Content not captured for this request.<br />
          <span class="muted">Content logging can be toggled with <code>TOTO_GW_LOG_CONTENT</code>.</span>
        </p>
      {/if}
    </div>
  {/if}
</Modal>

<style>
  /* mirror .field label (app.css) for the SegmentedControl's caption */
  .lbl {
    display: block;
    font-size: 0.6875rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text-3);
    font-weight: calc(600 + (var(--ui-weight) - 400));
    margin-bottom: 6px;
  }
  .clickrow {
    cursor: pointer;
  }
  /* reason sub-line under the task chip — only rendered when it adds info beyond the label */
  .rx {
    display: block;
    margin-top: 3px;
    font-size: 0.75rem;
    color: var(--text-3);
  }
  .clickrow:hover {
    background: var(--panel-2);
  }
  .est {
    color: var(--text-3);
    margin-left: 1px;
  }
  .why {
    margin: 0 0 14px;
    font-size: 0.8125rem;
    color: var(--text);
    line-height: 1.55;
  }
  .detail {
    margin: 0;
    display: grid;
    gap: 0;
  }
  .drow {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    padding: 8px 0;
    border-top: 1px solid var(--line);
    font-size: 0.8125rem;
  }
  .drow dt {
    color: var(--text-3);
    flex: 0 0 auto;
  }
  .drow dd {
    margin: 0;
    color: var(--text);
    text-align: right;
  }
  .note {
    margin: 14px 0 0;
    font-size: 0.75rem;
    color: var(--text-3);
    line-height: 1.5;
  }
  .content {
    margin-top: 14px;
    border-top: 1px solid var(--line);
    padding-top: 4px;
  }
  .content code {
    font-size: 0.72rem;
  }
  .cblock {
    margin-top: 12px;
  }
  .cblock h4 {
    margin: 0 0 6px;
    font-size: 0.6875rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text-3);
    font-weight: calc(600 + (var(--ui-weight) - 400));
  }
  .scroll {
    max-height: 220px;
    overflow: auto;
    background: var(--panel-2);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 10px 12px;
    font-size: 0.75rem;
    line-height: 1.5;
  }
  .msg + .msg {
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px solid var(--line);
  }
  .msg .role {
    display: block;
    color: var(--text-3);
    text-transform: uppercase;
    font-size: 0.625rem;
    letter-spacing: 0.06em;
    margin-bottom: 3px;
  }
  .scroll pre {
    margin: 0;
    white-space: pre-wrap;
    word-break: break-word;
    color: var(--text);
    font: inherit;
  }
</style>
