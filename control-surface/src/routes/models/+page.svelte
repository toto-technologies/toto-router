<script>
  // Models — the locked v18 page: serif hero, the discovery Library (both sources merged,
  // searchable/filterable), the Custom Models fine-tune table (tuning lineage ⋈ catalog ⋈
  // Fireworks-sync GPU state), and the Selecting-a-Model drawer (the details/advanced surface;
  // the card header populates it). Add to Catalog is one click on the card — a server-side
  // adoption, live immediately; the YAML file workflow lives behind the drawer's Advanced
  // disclosure. Library renders regardless of org scope; only the tuning table needs an org
  // for the operator credential (inline picker, tuning-page pattern).
  import { browser } from '$app/environment';
  // Inlined edition check (not $lib/edition.js) so the branch folds at build time — vite.config.js `define`.
  const OSS = typeof __EDITION__ !== 'undefined' && __EDITION__ === 'oss';
  import { tick } from 'svelte';
  import { query } from '$lib/api/resource.svelte.js';
  import { prettyModel, providerLabel } from '$lib/models.js';
  import { logoFor } from '$lib/logos.js';
  import {
    displayName,
    ctxShort,
    perM,
    suggestedId,
    orYaml,
    fwYaml,
    cfYaml,
    vendorFromSlug,
    vendorHue,
    catMark,
    syncFreshness,
    mergeDiscovery,
    filterDiscovery,
    DISCOVERY_FILTERS,
    FW_DISCOVERY_FILTERS,
    CF_DISCOVERY_FILTERS,
    ALL_DISCOVERY_FILTERS,
    kindLabel,
    customModelRows,
    withAdoptions,
    adoptionKey,
  } from '$lib/catalog.js';
  import { pct, money, fmtDuration } from '$lib/tuning.js';
  import {
    getOpenRouterDiscovery,
    getFireworksDiscovery,
    getCloudflareDiscovery,
    getCatalogModels,
    getFireworksSync,
    createAdoption,
    deleteAdoption,
    listAdoptions,
    getOrgRoutingPolicy,
    getTuningDatasets,
    getTuningJobs,
    getTuningModels,
    getTuningEvals,
  } from '$lib/api/admin.js';
  import ModelCard from '$lib/components/ModelCard.svelte';
  import SkeletonTable from '$lib/components/SkeletonTable.svelte';
  import { revealIn } from '$lib/motion.js';

  // ---- org scope (operator credential names one; the Library never needs it) -----------------
  let orgId = $state(browser ? (new URLSearchParams(location.search).get('org_id') ?? '') : '');
  let orgDraft = $state('');

  // ---- queries — this page is ABOUT the library, so both sources fetch on mount --------------
  const orQ = query(() => getOpenRouterDiscovery(), { isEmpty: () => false });
  const fwQ = query(() => getFireworksDiscovery(), { isEmpty: () => false });
  const cfQ = query(() => getCloudflareDiscovery(), { isEmpty: () => false });
  const catQ = query(() => getCatalogModels(), { isEmpty: (d) => !d?.models?.length });
  const syncQ = query(() => getFireworksSync(), { isEmpty: () => false });
  // caller-scope adoptions — the "added by you" join; a failure just means no markers
  const adoptQ = query(() => listAdoptions(), { isEmpty: () => false });
  // ROUTES stacks join the org routing policy; any failure (incl. org_id_required) → null →
  // footer-right simply stays empty. Never a wall.
  const routingQ = query(() => getOrgRoutingPolicy(orgId || undefined), { isEmpty: () => false });
  // Fine-tune management (getTuning*) is enterprise-only; the Custom Models section is !OSS-gated
  // below and immediate:!OSS keeps its eager fetch from 404ing in OSS.
  const tuningQ = query(() => loadTuning(orgId || undefined), {
    isEmpty: (d) => !d.models.length,
    immediate: !OSS,
  });
  function loadTuning(org) {
    return Promise.all([
      getTuningModels(org),
      getTuningJobs(org),
      getTuningDatasets(org),
      getTuningEvals(org),
    ]).then(([m, j, d, e]) => ({
      models: m?.models ?? [],
      jobs: j?.jobs ?? [],
      datasets: d?.datasets ?? [],
      evals: e?.evals ?? [],
    }));
  }
  const needsOrg = $derived(tuningQ.status === 'error' && tuningQ.error?.code === 'org_id_required');
  function submitOrg() {
    if (!orgDraft.trim()) return;
    orgId = orgDraft.trim();
    tuningQ.reload();
    routingQ.reload();
  }
  function refreshSources() {
    orQ.reload();
    fwQ.reload();
    cfQ.reload();
    syncQ.reload();
  }

  // ---- One-click adoption ---------------------------------------------------------------------
  // The card is the feedback: the flip happens the moment the server answers (the answer carries
  // the catalog id), the button says Adding…/Removing… in between, and a background refetch
  // reconciles against the discovery/catalog truth. Failure = plain-language notice, no flip.
  let overrides = $state({}); // adoptionKey → {cataloged, catalog_id}
  let busyKeys = $state({}); // adoptionKey → true while a call is in flight
  let notice = $state('');
  function reconcile(src) {
    ({ fireworks: fwQ, cloudflare: cfQ }[src] ?? orQ).reload();
    catQ.reload();
    adoptQ.reload();
  }
  function adoptionNotice(e, m) {
    const name = m.name || prettyModel(m.slug);
    if (e?.status === 401) return 'Your session has expired — sign in again.';
    if (e?.status === 403) return 'Couldn’t change the catalog — you need admin access.';
    return `Couldn’t update ${name}: ${e?.message ?? 'unknown error'}.`;
  }
  async function addToCatalog(m) {
    const key = adoptionKey(m);
    busyKeys[key] = true;
    notice = '';
    try {
      const r = await createAdoption(m.source, m.slug);
      overrides[key] = { cataloged: true, catalog_id: r?.entry?.id ?? null };
      reconcile(m.source);
    } catch (e) {
      notice = adoptionNotice(e, m);
    } finally {
      delete busyKeys[key];
    }
  }
  async function removeFromCatalog(m) {
    const key = adoptionKey(m);
    busyKeys[key] = true;
    notice = '';
    try {
      await deleteAdoption(m.catalog_id);
      overrides[key] = { cataloged: false, catalog_id: null };
      reconcile(m.source);
    } catch (e) {
      if (e?.status === 404) {
        // already gone — the flip is still the truth
        overrides[key] = { cataloged: false, catalog_id: null };
        reconcile(m.source);
      } else {
        notice = adoptionNotice(e, m);
      }
    } finally {
      delete busyKeys[key];
    }
  }

  // ---- Library view-model ---------------------------------------------------------------------
  const LIB_CAP = 48;
  let search = $state('');
  let source = $state('all'); // 'all' | 'openrouter' | 'fireworks'
  let filters = $state(new Set());
  function toggleFilter(k) {
    const next = new Set(filters);
    if (next.has(k)) next.delete(k);
    else next.add(k);
    filters = next;
  }
  const orModels = $derived(orQ.data?.models ?? []);
  const fwModels = $derived(fwQ.data?.models ?? []);
  const cfModels = $derived(cfQ.data?.models ?? []);
  const merged = $derived(
    withAdoptions(
      mergeDiscovery(orModels, fwModels, cfModels),
      adoptQ.data?.adoptions?.map((a) => a.id),
      overrides
    )
  );
  const pool = $derived(source === 'all' ? merged : merged.filter((m) => m.source === source));
  const filtered = $derived(filterDiscovery(pool, search, filters));
  const shown = $derived(filtered.slice(0, LIB_CAP));
  const filterDefs = $derived(
    source === 'openrouter'
      ? DISCOVERY_FILTERS
      : source === 'fireworks'
        ? FW_DISCOVERY_FILTERS
        : source === 'cloudflare'
          ? CF_DISCOVERY_FILTERS
          : ALL_DISCOVERY_FILTERS
  );
  const catalogedCount = $derived(merged.filter((m) => m.cataloged).length);
  const libLoading = $derived(
    orQ.status === 'loading' && fwQ.status === 'loading' && cfQ.status === 'loading'
  );
  const libFresh = $derived(syncFreshness(orQ.data) || syncFreshness(fwQ.data) || syncFreshness(cfQ.data));
  // per-source health lives on the source tab itself: a short state in the chip, the full
  // explanation as the pane when that source is selected. Never prose appended to the count line.
  const sourceState = $derived({
    openrouter:
      orQ.status === 'error' || orQ.data?.error
        ? { short: 'offline', title: 'OpenRouter didn’t answer', msg: 'The gateway couldn’t reach OpenRouter. Check connectivity, then Refresh sources.' }
        : null,
    fireworks:
      fwQ.status === 'error' || fwQ.data?.error
        ? { short: 'offline', title: 'Fireworks didn’t answer', msg: 'The gateway couldn’t reach Fireworks. Check connectivity, then Refresh sources.' }
        : fwQ.data && !fwQ.data.key_present
          ? { short: 'no key', title: 'Fireworks isn’t connected', msg: 'Set FIREWORKS_API_KEY on the gateway to browse Fireworks models here.' }
          : null,
    cloudflare:
      cfQ.data && !cfQ.data.key_present
        ? { short: 'no key', title: 'Cloudflare isn’t connected', msg: 'Set CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID on the gateway to browse Cloudflare models here.' }
        : cfQ.status === 'error' || cfQ.data?.error
          ? { short: 'offline', title: 'Cloudflare didn’t answer', msg: 'The gateway couldn’t reach Cloudflare. Check connectivity, then Refresh sources.' }
          : null,
  });
  const selectedDegraded = $derived(source !== 'all' ? sourceState[source] : null);
  // provenance facts get their own quiet line under the counts instead of one crammed sentence
  const factLine = $derived(
    [
      libFresh || null,
      fwQ.data?.filtered_out ? `${fwQ.data.filtered_out} deprecated/embedding models hidden` : null,
      filtered.length > LIB_CAP ? `showing the first ${LIB_CAP} — refine the search to see the rest` : null,
    ]
      .filter(Boolean)
      .join(' · ')
  );
  const routing = $derived(routingQ.status === 'ok' ? routingQ.data : null);
  const catalogIds = $derived((catQ.data?.models ?? []).map((m) => m.id));

  // ---- Selecting a Model (details drawer as a page section) ------------------------------------
  let selected = $state(null); // {m, fw, id, yaml} — the details surface, no longer the adopt path
  let copied = $state(false);
  const PREFIX_FOR = { openrouter: 'or', fireworks: 'fw', cloudflare: 'cf' };
  const YAML_FOR = { openrouter: orYaml, fireworks: fwYaml, cloudflare: cfYaml };
  async function showDetails(m) {
    const id = suggestedId(m.slug, catalogIds, PREFIX_FOR[m.source] ?? 'or');
    selected = { m, source: m.source, id, yaml: (YAML_FOR[m.source] ?? orYaml)(m, id) };
    copied = false;
    await tick();
    document.getElementById('adopt')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  // the drawer tracks live catalog state — a click on its own Add/Remove flips it in place
  const selLive = $derived(
    selected
      ? (merged.find((x) => x.source === selected.m.source && x.slug === selected.m.slug) ?? selected.m)
      : null
  );
  async function copyYaml() {
    try {
      await navigator.clipboard.writeText(selected?.yaml ?? '');
      copied = true;
      setTimeout(() => (copied = false), 1400);
    } catch {
      /* clipboard blocked — the block is selectable by hand */
    }
  }

  // ---- Custom Models rows -----------------------------------------------------------------------
  const rows = $derived(
    tuningQ.status === 'ok' || tuningQ.status === 'empty'
      ? customModelRows(tuningQ.data, catQ.data?.models, syncQ.data)
      : []
  );

</script>

<svelte:head><title>Model Library · Toto Control</title></svelte:head>

<div class="pagehead">
  <div>
    <h1>Model Library</h1>
    <div class="sub">Adopt models here — they appear in your <a href="/catalog">Catalog</a>.</div>
  </div>
  <div class="right">
    <button class="btn" onclick={refreshSources}>Refresh sources</button>
    {#if !OSS}<a class="btn primary" href="/tuning">Train a model</a>{/if}
  </div>
</div>

{#if orQ.status === 'unauthed' || fwQ.status === 'unauthed'}
  {@render deadend('Sign in required', 'Your session has expired. Sign in to browse the model library.')}
{:else if orQ.status === 'forbidden' && fwQ.status === 'forbidden'}
  {@render deadend('Admin access needed', 'You need an admin or owner role to browse the model library.')}
{:else if libLoading}
  <SkeletonTable rows={5} cols={4} />
{:else}
  <div class="libbar">
    <input
      class="libsearch"
      type="search"
      placeholder="Search {merged.length} models by name, family, or slug…"
      bind:value={search}
      aria-label="Search the model library"
    />
    <div class="seg" role="tablist" aria-label="Source">
      {#each [['all', 'All sources', null], ['openrouter', 'OpenRouter', orModels.length], ['fireworks', 'Fireworks', fwModels.length], ['cloudflare', 'Cloudflare', cfModels.length]] as [key, label, count] (key)}
        <button class:on={source === key} aria-pressed={source === key} onclick={() => (source = key)}>
          {label}{#if sourceState[key]}&nbsp;· <span class="st">{sourceState[key].short}</span>{:else if count != null}&nbsp;· {count}{/if}
        </button>
      {/each}
    </div>
    {#each filterDefs as f (f.key)}
      <button class="fchip" class:on={filters.has(f.key)} aria-pressed={filters.has(f.key)} onclick={() => toggleFilter(f.key)}>
        {f.label}
      </button>
    {/each}
  </div>
  <p class="countline">
    <b>{filtered.length}</b> of {pool.length} models · <b>{catalogedCount}</b> in your catalog
  </p>
  {#if factLine}
    <p class="quiet factline">{factLine}</p>
  {/if}
  {#if notice}
    <p class="notice" role="status">{notice}</p>
  {/if}
  {#if selectedDegraded}
    {@render deadend(selectedDegraded.title, selectedDegraded.msg)}
  {:else if !filtered.length}
    <p class="quiet">No models match — clear a filter or the search.</p>
  {:else}
    <div class="libgrid" in:revealIn>
      {#each shown as m (m.source + m.slug)}
        <ModelCard
          {m}
          source={m.source}
          {routing}
          onadopt={addToCatalog}
          onremove={removeFromCatalog}
          ondetails={showDetails}
          busy={!!busyKeys[adoptionKey(m)]}
        />
      {/each}
    </div>
  {/if}
{/if}

<!-- ===== Custom Models (fine-tunes) — enterprise-only ===== -->
{#if !OSS}
<div class="mhead"><h2>Custom Models</h2></div>

{#if tuningQ.status === 'loading'}
  <SkeletonTable rows={2} cols={6} />
{:else if needsOrg}
  <div class="stub" in:revealIn>
    <div class="ic"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></svg></div>
    <b>Pick an organization</b>
    <p>The operator credential has no home org — name the org whose fine-tunes you want to see. The Library above is global.</p>
    <form class="orgform" onsubmit={(e) => { e.preventDefault(); submitOrg(); }}>
      <input placeholder="org id" bind:value={orgDraft} aria-label="Organization id" />
      <button class="btn small primary" type="submit">View</button>
    </form>
  </div>
{:else if tuningQ.status === 'unauthed' || tuningQ.status === 'forbidden'}
  {@render deadend('No access', 'Your role can’t view this org’s fine-tuned models.')}
{:else if tuningQ.status === 'error'}
  {@render deadend('Could not load fine-tunes', tuningQ.error?.message ?? 'Unknown error')}
{:else if !rows.length}
  <div class="stub" in:revealIn>
    <div class="ic"><svg viewBox="0 0 24 24"><path d="M6 4v6M6 14v6M12 4v10M12 18v2M18 4v2M18 10v10" /><circle cx="6" cy="12" r="2" /><circle cx="12" cy="16" r="2" /><circle cx="18" cy="8" r="2" /></svg></div>
    <b>No custom models yet</b>
    <p>Train a model on the Tuning page — it appears here with its full dataset → job → model lineage, eval results, and serving status.</p>
  </div>
{:else}
  <div class="card" in:revealIn>
    <div class="tablewrap">
      <table>
        <thead>
          <tr><th>Model</th><th>Kind</th><th>Context</th><th>Lineage</th><th>Best eval</th><th>Status</th></tr>
        </thead>
        <tbody>
          {#each rows as r (r.model.id)}
            <tr>
              <td>
                <div class="mrow" style="--ph:{vendorHue('fireworks')}">
                  <span class="pmark" aria-hidden="true">
                    {#if logoFor('fireworks')}{@html logoFor('fireworks')}{:else}{catMark('fireworks')}{/if}
                  </span>
                  <div>
                    <div class="rname">{r.cat ? displayName(r.cat) : prettyModel(r.model.id)}</div>
                    <div class="rid n">
                      {r.model.catalog_id || r.model.id}{r.cat?.aliases?.length ? ` · alias ${r.cat.aliases.join(', ')}` : ''}
                    </div>
                  </div>
                </div>
              </td>
              <td><span class="kchip">{kindLabel(r.job)}</span></td>
              <td class="n">{ctxShort(r.cat?.context_window)}</td>
              <td>
                <div class="lineage">
                  <div class="step"><span class="lb">dataset</span><span><b>{r.dataset?.id ?? '—'}</b>{r.dataset ? ` · seed ${r.dataset.seed}` : ''}</span></div>
                  <div class="step"><span class="lb">job</span><span><b>{r.job?.id ?? '—'}</b>{r.job ? ` · ${money(r.job.cost_actual_usd ?? r.job.cost_estimate_usd)} · ${fmtDuration(r.job.created_at, r.job.completed_at)}` : ''}</span></div>
                  <div class="step"><span class="lb">model</span><span>this model</span></div>
                </div>
              </td>
              <td>
                {#if r.bestEval}
                  <span class="evalpill">{pct(r.bestEval.match_rate)} doc-state match</span>
                {:else}
                  <span class="quiet">no evals</span>
                {/if}
              </td>
              <td>
                <div class="statuscol">
                  {#if r.cat}
                    <span class="chip good"><span class="d"></span>In catalog</span>
                  {:else}
                    <span class="chip"><span class="d"></span>Not in catalog</span>
                  {/if}
                  {#if r.gpu === 'ready'}
                    <span class="chip good"><span class="d"></span>Deployed · ready</span>
                  {:else if r.gpu === 'off'}
                    <span class="chip warnc"><span class="d"></span>GPU off — on-demand</span>
                  {/if}
                </div>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </div>
{/if}
{/if}

<!-- ===== Selecting a Model ===== -->
<div class="mhead" id="adopt"><h2>Selecting a Model</h2></div>

{#if !selLive}
  <p class="quiet">Click a model's name in the Library above for its details.</p>
{:else}
  {@const sm = selLive}
  <div class="card drawer" in:revealIn>
    <div class="dleft">
      <div class="mrow" style="--ph:{vendorHue(vendorFromSlug(sm.slug))}">
        <span class="pmark" aria-hidden="true">
          {#if logoFor(vendorFromSlug(sm.slug))}{@html logoFor(vendorFromSlug(sm.slug))}{:else}{catMark(vendorFromSlug(sm.slug))}{/if}
        </span>
        <div>
          <h3>{sm.name || prettyModel(sm.slug)}</h3>
          <div class="dvia">via {providerLabel(sm.source)} · {sm.slug}</div>
        </div>
      </div>
      <p class="desc">
        Adding a model to the catalog makes it routable, priced, and policy-controlled for your
        org — live the moment you add it, no redeploy.
      </p>
      <dl class="kv">
        <dt>Upstream</dt><dd class="n">{sm.slug}</dd>
        {#if sm.cataloged}
          <dt>Catalog id</dt><dd class="n">{sm.catalog_id ?? selected.id}</dd>
        {/if}
        {#if selected.source === 'openrouter'}
          <dt>Price</dt><dd>{perM(sm.price_in)} in · {perM(sm.price_out)} out per M tokens</dd>
        {/if}
        <dt>Context</dt><dd>{sm.context_window != null ? `${sm.context_window.toLocaleString()} tokens` : '—'}</dd>
        <dt>Routing</dt><dd>bind task types in Task routing once it's in the catalog</dd>
      </dl>
      <div class="dact">
        {#if !sm.cataloged}
          <button
            class="btn small primary"
            disabled={!!busyKeys[adoptionKey(sm)]}
            onclick={() => addToCatalog(sm)}
          >
            {busyKeys[adoptionKey(sm)] ? 'Adding…' : 'Add to Catalog'}
          </button>
        {:else if sm.adopted}
          <span class="chip good"><span class="d"></span>In your catalog</span>
          <button
            class="btn small"
            disabled={!!busyKeys[adoptionKey(sm)]}
            onclick={() => removeFromCatalog(sm)}
          >
            {busyKeys[adoptionKey(sm)] ? 'Removing…' : 'Remove'}
          </button>
        {:else}
          <span class="chip good"><span class="d"></span>In the shipped catalog</span>
        {/if}
      </div>
    </div>
    <div class="dright">
      <details class="adv">
        <summary>Advanced: config entry</summary>
        <p class="advnote">
          Prefer the file workflow? The same model as a
          catalog.{selected.source}.yaml entry — pinned at deploy time,
          with the id ↔ upstream pair locked in <span class="n">tests/test_catalog.py</span>.
        </p>
        <div class="yamlbox">
          <button class="copybtn" onclick={copyYaml}>{copied ? 'Copied ✓' : 'Copy YAML'}</button>
          <pre class="n"># append to catalog.{selected.source}.yaml, then redeploy
{selected.yaml}</pre>
        </div>
      </details>
    </div>
  </div>
{/if}

<div class="reltip" style="margin-top:14px">
  <svg viewBox="0 0 24 24"><path d="M12 8v5M12 16h.01" /><circle cx="12" cy="12" r="9" /></svg>
  <div>
    <b>Why a library, not just a catalog:</b> the catalog stays the curated, fail-closed allowlist
    the router dispatches from — the shipped base models plus what you've added. The library is
    the exploration surface over it: every model your connected providers offer, searchable, with
    the cataloged ones marked. One click adds a model for your org, live immediately; models you
    added remove just as easily, and the shipped base catalog stays put.
  </div>
</div>

{#snippet deadend(title, msg)}
  <div class="stub" in:revealIn>
    <div class="ic"><svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h10" /><circle cx="18" cy="18" r="2.4" /></svg></div>
    <b>{title}</b>
    <p>{msg}</p>
  </div>
{/snippet}

<style>
  .pagehead a.btn {
    text-decoration: none;
    display: inline-flex;
    align-items: center;
  }
  .btn.primary {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
  }
  /* dark accent is light sage — dark ink on it, per the mockup */
  @media (prefers-color-scheme: dark) {
    .btn.primary { color: #0e1813; }
  }
  :global(:root[data-theme='dark']) .btn.primary { color: #0e1813; }
  :global(:root[data-theme='light']) .btn.primary { color: #fff; }

  /* ---- section heads — the catalog page's secthead scale, below the 24px page title ---- */
  .mhead {
    margin: 56px 0 18px;
  }
  .mhead h2 {
    margin: 0;
    font-size: 1.25rem;
    font-weight: calc(680 + (var(--ui-weight) - 400));
    letter-spacing: -0.015em;
  }

  /* ---- library controls ---- */
  .libbar {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    margin: 0 0 14px;
  }
  .libsearch {
    flex: 1 1 240px;
    background: var(--panel);
    border: 1px solid var(--line-2);
    border-radius: 8px;
    height: 36px;
    padding: 0 13px;
    color: var(--text);
    font-family: var(--sans);
    font-size: 0.8125rem;
  }
  .libsearch::placeholder { color: var(--text-3); }
  .libsearch:focus { border-color: var(--accent-line); outline: none; }
  .seg {
    display: flex;
    background: var(--panel);
    border: 1px solid var(--line-2);
    border-radius: 8px;
    overflow: hidden;
    flex: none;
  }
  .seg button {
    border: 0;
    background: transparent;
    font-family: var(--sans);
    font-size: 0.75rem;
    font-weight: calc(600 + (var(--ui-weight) - 400));
    color: var(--text-2);
    padding: 9px 14px;
    cursor: pointer;
    border-right: 1px solid var(--line);
    white-space: nowrap;
  }
  .seg button:last-child { border-right: 0; }
  .seg button.on {
    background: var(--accent-soft);
    color: var(--accent);
  }
  .seg .st { color: var(--warn); }
  .fchip {
    border: 1px solid var(--line-2);
    background: var(--panel);
    color: var(--text-2);
    border-radius: 20px;
    padding: 5px 12px;
    cursor: pointer;
    font-size: 0.6875rem;
    font-weight: calc(600 + (var(--ui-weight) - 400));
    white-space: nowrap;
    transition: background 0.13s ease, border-color 0.13s ease, color 0.13s ease;
  }
  .fchip:hover { background: var(--panel-hi); color: var(--text); }
  .fchip.on {
    background: var(--accent-soft);
    border-color: var(--accent-line);
    color: var(--accent);
  }
  .countline {
    margin: 0 0 14px;
    font-size: 0.75rem;
    color: var(--text-3);
  }
  .countline b { color: var(--text-2); }
  .countline:has(+ .factline) { margin-bottom: 4px; }
  .factline { margin: 0 0 14px; }
  .quiet {
    margin: 0;
    font-size: 0.75rem;
    color: var(--text-3);
  }
  .notice {
    margin: 0 0 14px;
    font-size: 0.75rem;
    color: var(--crit);
  }
  .libgrid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(270px, 1fr));
    gap: 18px;
  }

  /* ---- custom models table ---- */
  .mrow {
    display: flex;
    gap: 12px;
    align-items: center;
  }
  .pmark {
    width: 30px;
    height: 30px;
    border-radius: 8px;
    flex: 0 0 auto;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-family: var(--sans);
    font-size: 0.75rem;
    font-weight: calc(700 + (var(--ui-weight) - 400));
    color: light-dark(hsl(var(--ph) 58% 30%), hsl(var(--ph) 52% 74%));
    background: light-dark(hsl(var(--ph) 55% 52% / 0.13), hsl(var(--ph) 50% 62% / 0.16));
    border: 1px solid light-dark(hsl(var(--ph) 48% 44% / 0.4), hsl(var(--ph) 52% 66% / 0.36));
  }
  .pmark :global(svg.plogo) {
    width: 16px;
    height: 16px;
    display: block;
  }
  .rname {
    font-weight: calc(640 + (var(--ui-weight) - 400));
    color: var(--text);
  }
  .rid {
    font-size: 0.71875rem;
    color: var(--text-3);
    margin-top: 1px;
  }
  .kchip {
    display: inline-flex;
    align-items: center;
    border-radius: 5px;
    font-size: 0.6875rem;
    font-weight: calc(600 + (var(--ui-weight) - 400));
    padding: 3px 8px;
    background: var(--panel-2);
    border: 1px solid var(--line);
    color: var(--text-2);
    white-space: nowrap;
  }
  .lineage {
    font-size: 0.75rem;
    color: var(--text-2);
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .lineage .step {
    white-space: nowrap;
    display: flex;
    gap: 10px;
    align-items: baseline;
  }
  .lineage .lb {
    font-size: 0.5625rem;
    font-weight: calc(600 + (var(--ui-weight) - 400));
    letter-spacing: 0.11em;
    text-transform: uppercase;
    color: var(--text-3);
    width: 50px;
    flex: none;
  }
  .lineage b { color: var(--text); font-weight: calc(600 + (var(--ui-weight) - 400)); }
  .evalpill {
    font-size: 0.75rem;
    font-weight: calc(700 + (var(--ui-weight) - 400));
    color: var(--good);
    background: var(--good-soft);
    border-radius: 5px;
    padding: 3px 8px;
    white-space: nowrap;
  }
  .statuscol {
    display: flex;
    flex-direction: column;
    gap: 6px;
    align-items: flex-start;
  }
  .chip.warnc {
    color: var(--warn);
    background: var(--warn-soft);
    border-color: transparent;
  }
  .chip.warnc .d { background: var(--warn); }
  .chip.good .d { background: var(--good); }

  /* ---- adopt drawer (page section) ---- */
  .drawer {
    display: grid;
    grid-template-columns: minmax(260px, 1fr) minmax(320px, 1.4fr);
  }
  .dleft {
    padding: 20px 22px;
    border-right: 1px solid var(--line);
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .dleft h3 {
    margin: 0;
    font-size: 1rem;
    font-weight: calc(700 + (var(--ui-weight) - 400));
    letter-spacing: -0.01em;
  }
  .dvia {
    font-size: 0.625rem;
    letter-spacing: 0.13em;
    text-transform: uppercase;
    color: var(--text-3);
    font-weight: calc(600 + (var(--ui-weight) - 400));
    margin-top: 3px;
    overflow-wrap: anywhere;
  }
  .desc {
    margin: 0;
    font-size: 0.8125rem;
    color: var(--text-2);
    line-height: 1.55;
  }
  .kv {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 4px 16px;
    margin: 0;
    font-size: 0.75rem;
  }
  .kv dt { color: var(--text-3); font-weight: calc(600 + (var(--ui-weight) - 400)); }
  .kv dd { margin: 0; color: var(--text-2); min-width: 0; overflow-wrap: anywhere; }
  .dact {
    display: flex;
    gap: 10px;
    align-items: center;
    margin-top: 2px;
  }
  .dright {
    padding: 20px 22px;
    min-width: 0;
  }
  .adv summary {
    cursor: pointer;
    font-size: 0.75rem;
    font-weight: calc(600 + (var(--ui-weight) - 400));
    color: var(--text-2);
    user-select: none;
  }
  .adv summary:hover { color: var(--text); }
  .adv[open] summary { margin-bottom: 10px; }
  .advnote {
    margin: 0 0 10px;
    font-size: 0.71875rem;
    color: var(--text-3);
    line-height: 1.5;
  }
  .yamlbox {
    background: var(--panel-2);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 18px 20px;
    position: relative;
    overflow-x: auto;
  }
  .yamlbox pre {
    margin: 0;
    font-size: 0.75rem;
    line-height: 1.65;
    color: var(--text);
    white-space: pre;
  }
  .copybtn {
    position: absolute;
    top: 12px;
    right: 12px;
    border: 1px solid var(--line-2);
    background: var(--panel);
    color: var(--text-2);
    border-radius: 6px;
    font-size: 0.71875rem;
    font-weight: calc(600 + (var(--ui-weight) - 400));
    padding: 5px 11px;
    cursor: pointer;
  }
  .copybtn:hover { background: var(--panel-hi); }

  /* ---- operator org picker (tuning-page pattern) ---- */
  .orgform {
    display: flex;
    gap: 8px;
    justify-content: center;
    margin-top: 14px;
  }
  .orgform input {
    background: var(--panel-2);
    border: 1px solid var(--line-2);
    border-radius: 8px;
    height: 30px;
    width: 160px;
    padding: 0 10px;
    color: var(--text);
    font-family: var(--mono);
    font-size: 0.75rem;
  }
  .orgform input:focus { border-color: var(--accent-line); outline: none; }

  @media (max-width: 760px) {
    .drawer { grid-template-columns: 1fr; }
    .dleft { border-right: 0; border-bottom: 1px solid var(--line); }
  }
</style>
