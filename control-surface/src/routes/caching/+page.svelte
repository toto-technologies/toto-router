<script>
  // Caching — plain-language control over the gateway's caching behavior, all riding the ONE
  // org-default routing-policy row (PUT full-replaces, so every save passes the routing surface
  // through unchanged via policyPassthrough):
  //   A · strategy: the cache knob bundle (auto-inject + warmth pinning + prewarm) as presets,
  //       with the raw knobs editable under Custom.
  //   B · session pinning: per-task-type stickiness holds (stick_ttls) with human framing.
  //   C · savings: a small honest stat off the trace store (cache-savings + cache-health).
  import { browser } from '$app/environment';
  import { query } from '$lib/api/resource.svelte.js';
  import {
    getOrgRoutingPolicy,
    putOrgRoutingPolicy,
    getCacheSavings,
    getCacheHealth,
  } from '$lib/api/admin.js';
  import { PRESETS, presetFor, strategyWrite, policyPassthrough, toCacheHealth } from '$lib/cache.js';
  import Toggle from '$lib/components/Toggle.svelte';
  import SkeletonTable from '$lib/components/SkeletonTable.svelte';
  import { fmtUsd, fmtCompact } from '$lib/usage.js';
  import { revealIn } from '$lib/motion.js';

  // Operator deep-link (?org_id=) mirrors the catalog page; the OSS operator auto-resolves to the
  // local org server-side, so the common self-hoster path needs no org at all.
  const orgId = browser ? (new URLSearchParams(location.search).get('org_id') ?? '') : '';

  const routingQ = query(() => getOrgRoutingPolicy(orgId || undefined));

  // Savings window: the trailing 30 days. Read-only observability; 503/no-traffic degrade quietly.
  const DAY = 86400;
  const now = Math.floor(Date.now() / 1000);
  const savingsQ = query(() => getCacheSavings({ from: now - 30 * DAY, to: now, orgId: orgId || undefined }));
  const healthQ = query(() => getCacheHealth({ from: now - 30 * DAY, to: now, orgId: orgId || undefined }));

  // ---- edit state, seeded once per policy version (the catalog page's concurrency idiom: a save
  // reload bumps `version` → re-seed; another editor's save shows up on the next load/save cycle) --
  let sel = $state(null); // 'off'|'balanced'|'max'|'custom'|null — null = inherited deploy defaults
  let knobs = $state({ autoInject: true, minMessages: 3, warmthRouting: true, prewarm: false });
  let stickSel = $state({}); // label -> hold seconds; absent = deploy default
  let seeded = '';
  $effect(() => {
    const rp = routingQ.data;
    if (!rp) return;
    const key = `v${rp.version}`;
    if (key === seeded) return;
    seeded = key;
    inheritReset = false;
    sel = presetFor(rp.cache);
    const c = rp.cache ?? {};
    // Fallbacks when a knob is inherited = the shipped deploy defaults (config.py: auto-cache on
    // after 3 messages, warmth pinning on, prewarm off) — what dispatch does when the key is absent.
    knobs = {
      autoInject: c.auto_inject ?? true,
      minMessages: c.auto_inject_min_messages ?? 3,
      warmthRouting: c.warmth_routing ?? true,
      prewarm: !!rp.prewarm,
    };
    stickSel = { ...(rp.stick_ttls ?? {}) };
  });

  function pickPreset(id) {
    sel = id;
    inheritReset = false;
    const p = PRESETS.find((x) => x.id === id);
    if (p) knobs = {
      autoInject: p.knobs.auto_inject,
      minMessages: p.knobs.auto_inject_min_messages ?? knobs.minMessages,
      warmthRouting: p.knobs.warmth_routing,
      prewarm: p.knobs.prewarm,
    };
  }
  // Editing any knob makes the strategy explicit ('custom') — presets are just knob bundles.
  function touch(patch) {
    knobs = { ...knobs, ...patch };
    sel = 'custom';
    inheritReset = false;
  }

  // Per-task-type holds. 0 = deploy default (key dropped); 1s expires before the next turn can
  // land, so routing re-picks every turn — the honest "off". Warmth pinning (section A) can still
  // lengthen a hold while a provider cache is live.
  const HOLD_OPTS = [
    [0, 'Default'],
    [1, 'Off — re-pick every turn'],
    [300, '5 minutes'],
    [900, '15 minutes'],
    [3600, '1 hour'],
    [14400, '4 hours'],
    [86400, '24 hours'],
  ];
  function pickHold(label, seconds) {
    const next = { ...stickSel };
    if (seconds > 0) next[label] = seconds;
    else delete next[label];
    stickSel = next;
  }
  const holdRows = $derived((routingQ.data?.labels ?? []).filter((r) => r.label !== 'redact'));

  // ---- save: full-replace PUT = everything already on the row + this page's edits --------------
  let inheritReset = $state(false); // "Reset to deploy defaults" pressed → save writes cache:{} + prewarm off
  function body() {
    const base = policyPassthrough(routingQ.data);
    base.stick_ttls = { ...stickSel };
    if (inheritReset) return { ...base, cache: {}, prewarm: false };
    if (sel === null) return base; // strategy untouched — stored values pass through
    const { cache, prewarm } = strategyWrite(sel, knobs);
    return { ...base, cache, prewarm };
  }
  function resetStrategy() {
    sel = null;
    inheritReset = true;
    knobs = { autoInject: true, minMessages: 3, warmthRouting: true, prewarm: false };
  }

  let saving = $state(false);
  let saveErr = $state(null);
  let savedTick = $state(false);
  async function save() {
    saving = true;
    saveErr = null;
    try {
      await putOrgRoutingPolicy(body(), orgId || undefined);
      await routingQ.reload(); // bumped version → re-seed from what the server actually stored
      savedTick = true;
      setTimeout(() => (savedTick = false), 1600);
    } catch (e) {
      saveErr = e?.message ?? 'Save failed';
    } finally {
      saving = false;
    }
  }

  const version = $derived(routingQ.data?.version ?? 0);
  const ready = $derived(routingQ.status === 'ok' || routingQ.status === 'empty');
  const health = $derived(toCacheHealth(healthQ.status === 'ok' ? healthQ.data : null));
  const savings = $derived(savingsQ.status === 'ok' ? savingsQ.data : null);
  const fmtHold = (s) => (HOLD_OPTS.find(([v]) => v === s)?.[1] ?? `${s}s`);
</script>

<svelte:head><title>Caching · Toto Control</title></svelte:head>

<div class="pagehead">
  <div>
    <h1>Caching</h1>
    <div class="sub">
      Keep conversations on warm models and cached prompts — faster turns, cheaper input tokens.
    </div>
  </div>
  <div class="right">
    <button class="btn small primary" disabled={saving || !ready} onclick={save}>
      {saving ? 'Saving…' : savedTick ? 'Saved ✓' : `Save · v${version}`}
    </button>
  </div>
</div>

{#if saveErr}
  <div class="reltip" style="border-color:var(--crit);background:var(--crit-soft)" in:revealIn>
    <svg viewBox="0 0 24 24"><path d="M12 8v5M12 16h.01" /><circle cx="12" cy="12" r="9" /></svg>
    <div><b>Save failed.</b> {saveErr} — reload to pick up another editor’s changes, then save again.</div>
  </div>
{/if}

{#if routingQ.status === 'loading'}
  <SkeletonTable rows={6} cols={3} />
{:else if routingQ.status === 'unauthed'}
  <div class="stub" in:revealIn><b>Sign in required</b><p>Your session has expired. Sign in to manage caching.</p></div>
{:else if routingQ.status === 'forbidden'}
  <div class="stub" in:revealIn><b>Admin access needed</b><p>You need an admin role to configure caching.</p></div>
{:else if routingQ.status === 'error'}
  <div class="stub" in:revealIn>
    <b>Could not load the caching policy</b>
    <p>{routingQ.error?.message ?? 'Unknown error'}{routingQ.error?.code === 'org_id_required' ? ' — operator credentials must name one with ?org_id=…' : ''}</p>
  </div>
{:else}

  <!-- ===== SECTION A · STRATEGY ===== -->
  <div class="secthead">
    <span class="sn">A</span>
    <h2>Strategy</h2>
    <span class="hint">
      How aggressively the gateway caches prompts with your providers. Pick a preset, or tune the
      knobs under Custom.
    </span>
  </div>

  <div class="card" style="margin-bottom:14px">
    <div class="ch">
      <h3>Cache strategy</h3>
      <span class="meta">{sel === null ? 'inherited deploy defaults' : sel === 'custom' ? 'custom' : sel}</span>
    </div>
    <div class="cb">
      <div class="presets">
        {#each PRESETS as p (p.id)}
          <button class="preset" class:on={sel === p.id} onclick={() => pickPreset(p.id)}>
            <b>{p.name}{#if p.recommended}<span class="rec">recommended</span>{/if}</b>
            <span>{p.blurb}</span>
          </button>
        {/each}
        <button class="preset" class:on={sel === 'custom'} onclick={() => { sel = 'custom'; inheritReset = false; }}>
          <b>Custom</b>
          <span>Set each knob yourself.</span>
        </button>
      </div>
      {#if sel === null}
        <div class="inherit">
          No explicit strategy is stored — this gateway uses its deploy defaults (environment
          variables): auto-cache on after 3 messages, warm-model pinning on, prewarm off.
          Picking a preset makes the choice explicit and survives redeploys.
        </div>
      {/if}

      <div class="knob">
        <div class="kt">
          <span class="kl">Auto-cache continuing conversations</span>
          <span class="kd">
            Adds a provider cache breakpoint to conversations that didn’t send one, so repeated
            turns re-read the prompt prefix at a fraction of input price. One-shot requests are
            never cached — the ~25% cache-write premium would never pay back.
          </span>
        </div>
        <Toggle checked={knobs.autoInject} label="Auto-cache continuing conversations"
          onchange={(v) => touch({ autoInject: v })} />
      </div>
      <div class="knob" class:dim={!knobs.autoInject}>
        <div class="kt">
          <span class="kl">Count as “continuing” after</span>
          <span class="kd">
            Conversations shorter than this stay uncached. Requests carrying tools always count as
            continuing — a tool call guarantees a follow-up.
          </span>
        </div>
        <select class="numsel" aria-label="Minimum messages before auto-cache"
          disabled={!knobs.autoInject}
          value={knobs.minMessages}
          onchange={(e) => touch({ minMessages: Number(e.currentTarget.value) })}>
          {#each [2, 3, 5, 10].includes(knobs.minMessages) ? [2, 3, 5, 10] : [2, 3, 5, 10, knobs.minMessages].sort((a, b) => a - b) as n}
            <option value={n}>{n} messages</option>
          {/each}
        </select>
      </div>
      <div class="knob">
        <div class="kt">
          <span class="kl">Warm-model pinning</span>
          <span class="kd">
            While a conversation’s provider cache is still warm, keep it on the model it has been
            using instead of letting routine benchmark refreshes drift it to a near-tie — switching
            would throw the warm cache away. Your explicit task-type bindings always win.
          </span>
        </div>
        <Toggle checked={knobs.warmthRouting} label="Warm-model pinning"
          onchange={(v) => touch({ warmthRouting: v })} />
      </div>
      <div class="knob">
        <div class="kt">
          <span class="kl">Prewarm</span>
          <span class="kd">
            Lets clients warm a conversation’s provider cache before the first real turn
            (<code>POST /v1/prewarm</code>). A latency tool, not a cost tool: you pay the same
            cache write a little earlier, plus one extra tiny request.
          </span>
        </div>
        <Toggle checked={knobs.prewarm} label="Prewarm" onchange={(v) => touch({ prewarm: v })} />
      </div>

      <div class="strategyfoot">
        {#if sel !== null}
          <button class="btn small" onclick={resetStrategy}>Reset to deploy defaults</button>
        {/if}
        <details class="jsonadv">
          <summary>Advanced · what Save writes</summary>
          <pre class="mono">{JSON.stringify(body(), null, 2)}</pre>
        </details>
      </div>
    </div>
  </div>

  <!-- ===== SECTION B · SESSION PINNING ===== -->
  <div class="secthead" style="margin-top:24px">
    <span class="sn">B</span>
    <h2>Session pinning</h2>
    <span class="hint">
      Keep a conversation on the same model while it’s active, so its prompt cache stays hot.
    </span>
  </div>

  <div class="card" style="margin-bottom:14px">
    <div class="ch">
      <h3>Hold per task type</h3>
      <span class="meta">default hold 15 min · sliding</span>
    </div>
    <div class="cb">
      <p class="lede">
        When smart routing picks a model for a conversation, the conversation stays pinned to that
        model for the hold window; every new turn slides the window. Longer holds keep long working
        sessions on one warm model; shorter holds let routing re-shop sooner. Clients that declare a
        session id are always held for 4 hours regardless.
      </p>
      <div class="tblwrap">
        <table>
          <thead><tr><th>Task type</th><th>What it covers</th><th style="width:200px">Hold</th></tr></thead>
          <tbody>
            {#each holdRows as row (row.label)}
              <tr>
                <td class="mono lbl">{row.label}{#if row.custom}<span class="chip ovr">custom</span>{/if}</td>
                <td class="desc">{row.desc ?? ''}</td>
                <td>
                  <select class="holdsel" aria-label="Hold for {row.label}"
                    value={stickSel[row.label] ?? 0}
                    onchange={(e) => pickHold(row.label, Number(e.currentTarget.value))}>
                    {#each HOLD_OPTS as [secs, opt] (secs)}
                      <option value={secs}>{opt}</option>
                    {/each}
                    {#if stickSel[row.label] && !HOLD_OPTS.some(([s]) => s === stickSel[row.label])}
                      <option value={stickSel[row.label]}>{stickSel[row.label]} s</option>
                    {/if}
                  </select>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- ===== SECTION C · SAVINGS ===== -->
  <div class="secthead" style="margin-top:24px">
    <span class="sn">C</span>
    <h2>Savings</h2>
    <span class="hint">What caching actually did over the last 30 days, from this gateway’s traces.</span>
  </div>

  {#if healthQ.status === 'loading' || savingsQ.status === 'loading'}
    <SkeletonTable rows={1} cols={4} />
  {:else if !health?.hasTraffic}
    <div class="card"><div class="cb quiet">
      No traffic in the window yet — savings show up here once conversations start flowing.
      {#if healthQ.status === 'error'}(trace store unavailable: {healthQ.error?.message}){/if}
    </div></div>
  {:else}
    <div class="kpis" in:revealIn>
      <div class="kpi card"><div class="cb">
        <div class="lab">Cached tokens read</div>
        <div class="big">{fmtCompact(health.tokensCached)}</div>
        <div class="sub">of prompt input, served from provider caches</div>
      </div></div>
      <div class="kpi card"><div class="cb">
        <div class="lab">Cache hit rate</div>
        <div class="big">{(health.hitRate * 100).toFixed(1)}%</div>
        <div class="sub">cached ÷ total prompt tokens, token-weighted</div>
      </div></div>
      <div class="kpi card"><div class="cb">
        <div class="lab">Warm-pin turns</div>
        <div class="big">{fmtCompact(health.warmHolds)}</div>
        <div class="sub">turns kept on a warm model by pinning</div>
      </div></div>
      <div class="kpi card"><div class="cb">
        <div class="lab">Net saved</div>
        <div class="big">{savings ? fmtUsd(savings.total?.net_usd ?? 0) : '—'}</div>
        <div class="sub">
          {#if savings}
            {fmtUsd(savings.total?.read_savings_usd ?? 0)} read savings −
            {fmtUsd(savings.total?.write_premium_usd ?? 0)} write premium
          {:else}
            savings breakdown unavailable
          {/if}
        </div>
      </div></div>
    </div>
  {/if}

{/if}

<style>
  .presets{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:9px;margin-bottom:14px}
  .preset{display:flex;flex-direction:column;gap:5px;text-align:left;padding:11px 13px;border:1px solid var(--line);
    border-radius:9px;background:var(--panel);cursor:pointer}
  .preset:hover{border-color:var(--text-3)}
  .preset.on{border-color:var(--accent);background:var(--accent-soft);box-shadow:0 0 0 1px var(--accent-line)}
  .preset b{font-size:0.8125rem;display:flex;align-items:center;gap:7px}
  .preset span{font-size:0.6875rem;color:var(--text-3);line-height:1.45}
  .rec{font-family:var(--mono);font-size:0.625rem;color:var(--accent);border:1px solid var(--accent-line);
    border-radius:4px;padding:1px 5px;text-transform:uppercase;letter-spacing:.05em}
  .inherit{font-size:0.75rem;color:var(--text-2);background:var(--accent-soft);border:1px solid var(--accent-line);
    border-radius:8px;padding:9px 12px;margin-bottom:12px;line-height:1.5}
  .knob{display:flex;align-items:center;gap:16px;padding:11px 2px;border-top:1px solid var(--line)}
  .knob.dim{opacity:.55}
  .kt{flex:1;display:flex;flex-direction:column;gap:3px}
  .kl{font-size:0.8125rem;font-weight:calc(600 + (var(--ui-weight) - 400))}
  .kd{font-size:0.6875rem;color:var(--text-3);line-height:1.5;max-width:56ch}
  .kd code{font-family:var(--mono);font-size:0.6875rem}
  .numsel,.holdsel{font-family:var(--sans);font-size:0.75rem;color:var(--text);background:var(--panel);
    border:1px solid var(--line);border-radius:7px;height:30px;padding:0 8px}
  .strategyfoot{display:flex;align-items:flex-start;gap:12px;margin-top:12px}
  .jsonadv{margin-left:auto;font-size:0.6875rem;color:var(--text-3)}
  .jsonadv summary{cursor:pointer;user-select:none}
  .jsonadv pre{margin:8px 0 0;padding:10px 12px;background:var(--panel-2, var(--panel));border:1px solid var(--line);
    border-radius:8px;font-size:0.6875rem;line-height:1.5;max-height:280px;overflow:auto;text-align:left}
  .lede{margin:0 0 12px;font-size:0.75rem;color:var(--text-2);line-height:1.55;max-width:78ch}
  .tblwrap{overflow-x:auto}
  table{width:100%;border-collapse:collapse}
  th{text-align:left;font-size:0.6875rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text-3);
    font-weight:500;padding:7px 10px;border-bottom:1px solid var(--line)}
  td{padding:8px 10px;border-bottom:1px solid var(--line);font-size:0.75rem;vertical-align:middle}
  tr:last-child td{border-bottom:0}
  td.lbl{white-space:nowrap}
  td.lbl .chip{margin-left:7px}
  td.desc{color:var(--text-3);font-size:0.6875rem;max-width:52ch}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
  .kpi .lab{font-size:0.6875rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text-3)}
  .quiet{color:var(--text-3);font-size:0.75rem}
</style>
