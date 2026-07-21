<script>
  // Catalog & Routing — the flagship control-surface page. Ports the a-forest mockup's screen and
  // wires it to the live admin API. The screen scopes to the org-default routing policy or ONE team;
  // switching scope re-queries routing, and team scopes also re-query catalog policy.
  //   A · task routing: per-task-type model bindings + the Optimize preset (PUT on save).
  //   B · model catalog: the full catalog as provider modules (/v1/admin/catalog/models), read-only,
  //       hue+monogram identity shared with the Benchmarks page. (The discovery LIBRARY lives on
  //       /models — this page is the curated catalog view.)
  //   C · Fireworks sync: account ⇄ catalog reconciliation (/v1/admin/catalog/sync/fireworks) with
  //       Adopt / Fix-ref YAML hand-offs. Always-200 contract — key-missing/upstream-fail are data.
  //   D · team access (team scope only): allow/deny + residency + team default (PUT on save).
  // "Add task type" posts a custom_labels entry (CT).
  import { browser } from '$app/environment';
  import { query } from '$lib/api/resource.svelte.js';
  import { prettyModel, providerLabel, newerCheaper } from '$lib/models.js';
  import { taskLabel } from '$lib/tasks.js';
  import { perMtok, fmtMtok } from '$lib/pricing.js';
  import { loadTrafficRows, trafficStats } from '$lib/traffic.js';
  import { providerHue, providerMark } from '$lib/benchmarks.js';
  import { logoFor } from '$lib/logos.js';
  import {
    groupByProvider,
    displayName,
    upstreamParts,
    ctxShort,
    servingLabel,
    driftCounts,
    driftSummary,
    driftSentence,
    driftAction,
    syncFreshness,
    lastSeg,
  } from '$lib/catalog.js';
  import {
    listTeams,
    getRoutingPolicy,
    putRoutingPolicy,
    getOrgRoutingPolicy,
    putOrgRoutingPolicy,
    getCatalogPolicy,
    putCatalogPolicy,
    getEffectiveModels,
    getCatalogModels,
    getFireworksSync,
    deleteAdoption,
  } from '$lib/api/admin.js';
  import AddLocalModel from '$lib/components/AddLocalModel.svelte';
  import { toFailMatrix, failPolicyBody } from '$lib/failpolicy.js';
  import ModelPicker from '$lib/components/ModelPicker.svelte';
  import SegmentedControl from '$lib/components/SegmentedControl.svelte';
  import Toggle from '$lib/components/Toggle.svelte';
  import Modal from '$lib/components/Modal.svelte';
  import SkeletonTable from '$lib/components/SkeletonTable.svelte';
  import { revealIn } from '$lib/motion.js';

  // Inlined edition check (not $lib/edition.js) so the team-scope branches fold at build time —
  // vite.config.js `define`. OSS is single-tenant: no teams, so the org-default policy is the
  // whole surface and the team switcher/access sections drop out.
  const OSS = typeof __EDITION__ !== 'undefined' && __EDITION__ === 'oss';

  const OPTHINT = {
    quality: 'Breaks ties toward the stronger model on the fallback path — bound task types are unaffected.',
    balanced: 'Breaks ties toward the best price-for-quality model on the fallback path.',
    cost: 'Breaks ties toward the cheaper comparable model on the fallback path.',
  };
  const OPT_OPTS = [
    { value: 'quality', label: 'Quality' },
    { value: 'balanced', label: 'Balanced' },
    { value: 'cost', label: 'Cost' },
  ];
  // Org-only: what happens when smart routing itself is unavailable, PER failure reason (W2-C7).
  const FAIL_OPTS = [
    { value: 'open', label: 'Keep serving' },
    { value: 'closed', label: 'Reject requests' },
  ];
  // The backend's FAIL_REASONS (routes/admin_routing.py) in plain language, in a stable render order.
  const FAIL_REASONS = [
    { key: 'classify_failed', label: 'Classifier down', hint: 'The task classifier can’t label the request.' },
    { key: 'breaker_open', label: 'Provider circuit open', hint: 'The chosen model’s provider has tripped its circuit breaker.' },
    { key: 'policy_error', label: 'Policy error', hint: 'This routing policy failed to evaluate.' },
  ];

  // ---- Queries -------------------------------------------------------------------------------
  // ORG is a scope sentinel: the org-DEFAULT routing policy (what a teamless owner / pi traffic
  // resolves). It's the default selection because it governs the operator's OWN smart-routed
  // requests; specific teams are still selectable. Catalog allow/deny stays team-only (no org
  // catalog endpoint) so that section hides in org scope.
  const ORG = '__org__';
  // Operator credential has no home org and must name one for the routing/team reads (?org_id=
  // deep-link, or the inline picker on org_id_required) — same pattern as the tuning page.
  // Sections B/C are global reads and never need it.
  let orgId = $state(browser ? (new URLSearchParams(location.search).get('org_id') ?? '') : '');
  let orgDraft = $state('');
  // listTeams (/v1/admin/teams) is enterprise-only; OSS never fetches it and stays pinned to the
  // org-default scope (teamId = ORG), which renders the routing surface directly.
  const teamsQ = query(() => listTeams(orgId || undefined), { immediate: !OSS });
  let teamId = $state(ORG);
  // Bindable options = the EDITED scope's effective catalog (base + its adoptions) — exactly
  // the set the routing-policy PUT accepts and dispatch resolves for that scope's callers.
  // /v1/models was wrong here: caller-pinned (operator sees no adoptions) and scope-blind.
  const modelsQ = query(() =>
    getEffectiveModels(teamId === ORG ? { orgId: orgId || undefined } : { teamId, orgId: orgId || undefined }));
  const isOrg = $derived(teamId === ORG);
  const routingQ = query(
    () =>
      teamId === ORG
        ? getOrgRoutingPolicy(orgId || undefined)
        : getRoutingPolicy(teamId, orgId || undefined),
    { immediate: false }
  );
  const catalogQ = query(() => getCatalogPolicy(teamId, orgId || undefined), { immediate: false });
  const needsOrg = $derived(teamsQ.status === 'error' && teamsQ.error?.code === 'org_id_required');
  function submitOrg() {
    if (!orgDraft.trim()) return;
    orgId = orgDraft.trim();
    teamsQ.reload();
    routingQ.reload();
    modelsQ.reload();
    if (teamId !== ORG) catalogQ.reload();
  }

  // Scope change re-loads routing (+ catalog for a real team; org has no catalog policy).
  $effect(() => {
    if (teamId) {
      routingQ.reload();
      modelsQ.reload();
      if (teamId !== ORG) catalogQ.reload();
    }
  });

  const teams = $derived(teamsQ.data?.teams ?? []);
  const team = $derived(teams.find((t) => t.team_id === teamId) ?? null);
  const teamName = $derived(OSS ? 'this gateway' : isOrg ? 'Organization (default)' : (team?.name ?? '—'));
  const emblem = $derived(isOrg ? '⌂' : (team?.name ?? '··').slice(0, 2).toUpperCase());
  const scopeReady = $derived(isOrg || !!team);

  // ---- Editable local state (seeded from the resolved policies) ------------------------------
  let optimize = $state('balanced');
  // org-only per-reason matrix: reason -> 'open'|'closed'. A scalar from the API expands to all-same;
  // it collapses back to a scalar on save when the rows agree (round-trip compat with the API default).
  let failMatrix = $state({ classify_failed: 'open', breaker_open: 'open', policy_error: 'open' });
  let classifierModel = $state(''); // org-only: the model that reads prompts to route them ('' = default)
  let routeSel = $state({}); // label/custom-name -> chosen model id
  let stickSel = $state({}); // label -> hold seconds (0/absent = default); the stickiness lever
  let allowed = $state(new Set()); // catalog model ids the team may use
  let defaultModel = $state(null); // team default (the star)
  let resLocal = $state(true); // allow in-perimeter
  let resCloud = $state(true); // allow cloud
  let providersOn = $state(new Set()); // provider chips (visual — no backend field)
  let customLabels = $state([]); // [{name, desc, model}]
  // Data classification (W2-C7, org scope): editable rows [{name, desc, constraint}] + a default
  // label. Constraint is 'allow' | 'local_only' | 'deny' (plain-language selects below).
  let taxLabels = $state([]);
  let taxDefault = $state('');
  let catalogCollapsed = $state(false);
  let seedKey = $state(null);

  const modelList = $derived(modelsQ.data?.models ?? []);
  const allModelIds = $derived(modelList.map((m) => m.id));
  const modelById = $derived(new Map(modelList.map((m) => [m.id, m])));

  // ---- Model display: real provider name is the identity; or-* alias is the demoted routing key ----
  // prettyModel + providerLabel come from $lib/models.js (shared with the Benchmarks page).

  // "anthropic/claude-sonnet-5" → "Claude Sonnet 5"; "qwen2.5-coder-32b-instruct" → "Qwen2.5 Coder 32b Instruct".
  // Split on - and _ only (never on '.', which lives inside version numbers). Falls back to the id.
  const prettyId = (id) => (modelById.has(id) ? prettyModel(modelById.get(id)) : id);

  // One price unit everywhere on this page: $ per 1M tokens (perMtok/fmtMtok from $lib/pricing.js,
  // shared with the Models page) — the API stores per-1k, humans compare per-Mtok.
  const mtok = (n) => fmtMtok(perMtok(n));

  // Observed traffic (this gateway's own request log) behind the picker's compare mode: per-model
  // p50 latency + per-task-type token/volume averages. Loaded once; a failed read degrades to
  // empty maps (pickers then show the cold-start static prices, never a fabricated estimate).
  let traffic = $state(null);
  $effect(() => {
    if (!browser || traffic) return;
    loadTrafficRows()
      .then((rows) => (traffic = trafficStats(rows)))
      .catch(() => (traffic = { perModel: new Map(), perLabel: new Map() }));
  });
  const newerSet = $derived(newerCheaper(modelList));

  // Compare-footer hand-off: jump to the model's row in the Model catalog section below.
  function scrollToCatalog(id) {
    document.getElementById(`cat-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  // Price signal (cheap→premium) from the blended $/1k. Terciles relative to the catalog's own max —
  // ponytail: self-calibrating so it stays sane across providers; swap for absolute $ bands if the
  // catalog ever spans orders of magnitude and the buckets stop separating.
  const blended = (m) => (m?.price_in == null && m?.price_out == null ? null : (m.price_in ?? 0) + (m.price_out ?? 0));
  const maxBlend = $derived(Math.max(0, ...modelList.map((m) => blended(m) ?? 0)));
  function priceTier(m) {
    const b = blended(m);
    if (b == null || maxBlend <= 0) return null;
    const r = b / maxBlend;
    return r < 0.34 ? 'cheap' : r < 0.67 ? 'mid' : 'premium';
  }

  // Sort the catalog table by price (header toggles asc→desc→off). Unpriced rows sink to the bottom.
  let priceSort = $state(null); // null | 'asc' | 'desc'
  const cyclePriceSort = () => (priceSort = priceSort == null ? 'asc' : priceSort === 'asc' ? 'desc' : null);
  const sortedModels = $derived.by(() => {
    if (!priceSort) return modelList;
    const dir = priceSort === 'asc' ? 1 : -1;
    return [...modelList].sort((a, b) => {
      const x = blended(a), y = blended(b);
      if (x == null) return 1;
      if (y == null) return -1;
      return (x - y) * dir;
    });
  });

  const ready = (q) => q.status === 'ok' || q.status === 'empty';

  $effect(() => {
    // Seed edit state once per (team, routing-version, catalog-version). Re-seeds after a save
    // reload bumps a version. Writing edit state never bumps a version → no loop.
    // Org scope has no catalog policy — seed on routing + models alone there.
    if (!(ready(routingQ) && ready(modelsQ) && (isOrg || ready(catalogQ)))) return;
    const key = `${teamId}:${routingQ.data?.version}:${catalogQ.data?.version}`;
    if (key === seedKey) return;
    seedKey = key;
    seed();
  });

  function seed() {
    const rp = routingQ.data;
    // Org scope has no catalog policy — ignore catalogQ.data, which still holds the previously
    // selected TEAM's policy after a Team → Org switch (its allow/deny must not gate org routing).
    const cp = isOrg ? null : catalogQ.data;
    optimize = rp?.optimize ?? 'balanced';
    failMatrix = toFailMatrix(rp?.fail_policy);
    classifierModel = rp?.classifier_model ?? '';
    const sel = {};
    for (const row of rp?.labels ?? []) if (row.bindable) sel[row.label] = row.model;
    routeSel = sel;
    stickSel = { ...(rp?.stick_ttls ?? {}) }; // per-task-type hold (empty = flat default)
    customLabels = (rp?.custom_labels ?? []).map((c) => ({ ...c }));
    // Data classification (W2-C7): flatten the stored {labels:{name:{constraint,desc}}, default}
    // into editable rows.
    const tx = rp?.taxonomy ?? {};
    taxLabels = Object.entries(tx.labels ?? {}).map(([name, r]) => ({
      name, desc: r?.desc ?? '', constraint: r?.constraint ?? 'allow',
    }));
    taxDefault = tx.default ?? '';

    // Catalog: version 0 (unset) is permissive — everything allowed.
    const ids = allModelIds;
    if (!cp || cp.version === 0) allowed = new Set(ids);
    else if (cp.mode === 'deny') {
      const deny = new Set(cp.models ?? []);
      allowed = new Set(ids.filter((id) => !deny.has(id)));
    } else allowed = new Set(cp.models ?? []);
    defaultModel = cp?.default_model ?? null;
    const res = cp?.residency;
    resLocal = res == null || res.includes('in_perimeter');
    resCloud = res == null || res.includes('cloud');
    providersOn = new Set(modelList.map((m) => m.via).filter(Boolean));
  }

  // ---- Derived views -------------------------------------------------------------------------
  const bindableCount = $derived((routingQ.data?.labels ?? []).filter((r) => r.bindable).length);
  const allowedCount = $derived(allModelIds.filter((id) => allowed.has(id)).length);
  const providerVias = $derived([...new Set(modelList.map((m) => m.via).filter(Boolean))]);
  const optHint = $derived(OPTHINT[optimize] ?? '');
  const version = $derived(routingQ.data?.version ?? 0);

  function isOver(row) {
    return row.custom || (routeSel[row.label] ?? row.model) !== row.default_model;
  }
  function resetRow(row) {
    routeSel = { ...routeSel, [row.label]: row.default_model };
  }
  function pickModel(label, value) {
    routeSel = { ...routeSel, [label]: value };
  }
  // Stickiness hold options (plain language): how long a conversation stays pinned to its first
  // routed model. "Default" (0) drops the label from stick_ttls → the deploy-wide hold applies.
  const HOLD_OPTS = [
    [0, 'Default'],
    [300, '5 min'],
    [900, '15 min'],
    [3600, '1 hour'],
    [14400, '4 hours'],
  ];
  function pickHold(label, seconds) {
    const next = { ...stickSel };
    if (seconds > 0) next[label] = seconds;
    else delete next[label];
    stickSel = next;
  }
  function toggleAllow(id) {
    const next = new Set(allowed);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    allowed = next;
  }
  function setDefault(id) {
    defaultModel = defaultModel === id ? null : id;
  }
  function toggleProvider(v) {
    const next = new Set(providersOn);
    if (next.has(v)) next.delete(v);
    else next.add(v);
    providersOn = next;
  }
  const ctxFmt = (n) => (n == null ? '—' : n >= 1000 ? `${Math.round(n / 1000)}K` : `${n}`);

  // ---- Data classification (W2-C7) -----------------------------------------------------------
  // Plain-language constraint choices → the stored enum. Order is severity-ascending.
  const CONSTRAINT_OPTS = [
    ['allow', 'No restriction'],
    ['local_only', 'Keep on in-perimeter models'],
    ['deny', 'Block'],
  ];
  function addTaxRow() {
    taxLabels = [...taxLabels, { name: '', desc: '', constraint: 'allow' }];
  }
  function removeTaxRow(i) {
    const removed = taxLabels[i]?.name;
    taxLabels = taxLabels.filter((_, j) => j !== i);
    if (removed && removed === taxDefault) taxDefault = '';
  }
  // The stored taxonomy object from the editable rows (drops unnamed rows; a default that no longer
  // names a live label is cleared, matching the backend's fail-closed validation).
  function taxonomyBody() {
    const labels = {};
    for (const r of taxLabels) {
      const name = (r.name ?? '').trim();
      if (!name) continue;
      labels[name] = { constraint: r.constraint, desc: (r.desc ?? '').trim() };
    }
    if (!Object.keys(labels).length) return {};
    return { labels, default: taxDefault && labels[taxDefault] ? taxDefault : null };
  }
  const taxNames = $derived(taxLabels.map((r) => (r.name ?? '').trim()).filter(Boolean));

  // ---- Classifier picker (W3-C1) -------------------------------------------------------------
  // The model that reads each prompt to route it (rendered by ModelPicker, which shows residency
  // per row — in-perimeter is the safe pick for a data-restricted org).
  const isPerim = (id) => modelById.get(id)?.residency_class === 'in_perimeter';
  // Any local_only/deny label means the classifier itself must stay in-perimeter (it reads the prompt
  // before the residency guard). Mirror the backend guard so a bad pick is flagged before Save 422s.
  const taxRequiresLocal = $derived(taxLabels.some((r) => r.constraint === 'local_only' || r.constraint === 'deny'));
  const classifierLeaks = $derived(!!classifierModel && !isPerim(classifierModel) && taxRequiresLocal);

  // ---- Save ----------------------------------------------------------------------------------
  function routingBody(customList) {
    const bindings = {};
    for (const row of routingQ.data?.labels ?? []) {
      if (row.custom || !row.bindable) continue;
      const sel = routeSel[row.label];
      if (sel && sel !== row.default_model) bindings[row.label] = sel; // only overrides ride the overlay
    }
    const custom_labels = customList.map((c) => ({
      name: c.name,
      desc: c.desc,
      model: routeSel[c.name] ?? c.model,
    }));
    // Only non-default holds ride the overlay (a 0/absent entry means "use the deploy default").
    const stick_ttls = {};
    for (const [label, secs] of Object.entries(stickSel)) if (secs > 0) stick_ttls[label] = secs;
    // prewarm + the cache strategy live on this same policy row but are edited on the Cache page —
    // the PUT full-replaces, so send them through unchanged or a routing Save wipes them.
    return {
      bindings, optimize, custom_labels, stick_ttls,
      prewarm: !!routingQ.data?.prewarm,
      cache: { ...(routingQ.data?.cache ?? {}) },
      // full-replace: the org toggle sends its edited value; a team save (no UI for it) passes the
      // stored value through so neither path silently resets fail_policy to 'open'. undefined drops.
      fail_policy: isOrg ? failPolicyBody(failMatrix) : routingQ.data?.fail_policy,
      // W2-C7 data classification is an ORG-scope control; org sends the edited taxonomy, a team save
      // passes the stored value through so neither path silently wipes it (the fail_policy trap).
      taxonomy: isOrg ? taxonomyBody() : (routingQ.data?.taxonomy ?? {}),
      // W3-C1 classifier is ORG-ONLY (the backend rejects it on a team PUT); org sends its choice
      // ('' → null clears back to the gateway default), a team save omits the key entirely.
      ...(isOrg ? { classifier_model: classifierModel || null } : {}),
    };
  }
  function catalogBody() {
    const models = allModelIds.filter((id) => allowed.has(id));
    const residency =
      resLocal && resCloud
        ? null
        : [...(resLocal ? ['in_perimeter'] : []), ...(resCloud ? ['cloud'] : [])];
    return { mode: 'allow', models, residency, default_model: defaultModel };
  }

  let saving = $state(false);
  let saveErr = $state(null);
  async function save() {
    saving = true;
    saveErr = null;
    try {
      if (isOrg) {
        await putOrgRoutingPolicy(routingBody(customLabels), orgId || undefined);
        await routingQ.reload(); // org scope has no catalog policy
      } else {
        await putRoutingPolicy(teamId, routingBody(customLabels), orgId || undefined);
        await putCatalogPolicy(teamId, catalogBody(), orgId || undefined);
        await Promise.all([routingQ.reload(), catalogQ.reload()]); // bumped versions → re-seed
      }
    } catch (e) {
      saveErr = e?.message ?? 'Save failed';
    } finally {
      saving = false;
    }
  }

  // ---- Add / edit custom task type (CT) modal ------------------------------------------------
  let addOpen = $state(false);
  let ctName = $state('');
  let ctDesc = $state('');
  let ctModel = $state('');
  let ctErr = $state(null); // {field?, message}
  let ctSaving = $state(false);
  let ctEditing = $state(null); // name of the custom task type being edited (null = adding)

  // Backend 400 codes → which field the inline error hangs under.
  const CT_FIELD = {
    invalid_custom_label_name: 'name',
    custom_label_collision: 'name',
    duplicate_custom_label: 'name',
    invalid_custom_label_desc: 'desc',
    unknown_model: 'model',
  };

  // Authoring aids: built-in descriptions as reference material (a curated handful, not the whole
  // table), and a soft thin-description warning. The backend hard-rejects under 3 words; the UI
  // nudges earlier, at under 5 — client-side only, no classifier calls.
  const CT_EXEMPLARS = ['brainstorming', 'classification', 'summarization', 'code_generation', 'extraction'];
  const ctExamples = $derived((routingQ.data?.labels ?? [])
    .filter((r) => CT_EXEMPLARS.includes(r.label) && r.desc));
  const ctWords = $derived(ctDesc.trim() ? ctDesc.trim().split(/\s+/).length : 0);
  const ctThin = $derived(ctWords > 0 && ctWords < 5);

  function openAdd() {
    ctEditing = null;
    ctName = '';
    ctDesc = '';
    // Prefer a REAL allowed model — an echo/fake test entry is never a sensible binding default.
    const pickable = allModelIds.filter((id) => allowed.has(id));
    ctModel = pickable.find((id) => modelById.get(id)?.endpoint !== 'fake')
      ?? pickable[0] ?? allModelIds[0] ?? '';
    ctErr = null;
    addOpen = true;
  }
  function openEdit(name) {
    const c = customLabels.find((c) => c.name === name);
    if (!c) return;
    ctEditing = c.name;
    ctName = c.name;
    ctDesc = c.desc;
    ctModel = routeSel[c.name] ?? c.model;
    ctErr = null;
    addOpen = true;
  }
  // Shared save for add / edit / remove: PUT the policy with `list` as the custom set, then
  // reload so the re-seed reflects server truth (same contract the Add flow always had).
  async function saveCustomList(list) {
    const body = routingBody(list);
    if (isOrg) {
      await putOrgRoutingPolicy(body, orgId || undefined);
      await routingQ.reload();
    } else {
      await putRoutingPolicy(teamId, body, orgId || undefined);
      await Promise.all([routingQ.reload(), catalogQ.reload()]);
    }
  }
  async function submitCustom() {
    ctSaving = true;
    ctErr = null;
    const entry = { name: ctName.trim(), desc: ctDesc.trim(), model: ctModel };
    // The table dropdown may hold a stale pick for this name — the modal's choice wins
    // (routingBody reads routeSel first for custom rows).
    routeSel = { ...routeSel, [entry.name]: entry.model };
    try {
      await saveCustomList(
        ctEditing ? customLabels.map((c) => (c.name === ctEditing ? entry : c))
                  : [...customLabels, entry]);
      addOpen = false;
    } catch (e) {
      ctErr = { field: CT_FIELD[e?.code], message: e?.message ?? 'Could not save task type' };
    } finally {
      ctSaving = false;
    }
  }

  // Remove is two-click (arm, then confirm) and saves immediately, mirroring Add.
  let removeArm = $state(null); // custom task-type name armed for removal
  let removing = $state(null);
  async function removeCustom(name) {
    if (removeArm !== name) {
      removeArm = name;
      return;
    }
    removeArm = null;
    removing = name;
    // Drop the label's per-type hold too — the PUT rejects a stick_ttls key for a label the
    // body no longer defines.
    const holds = { ...stickSel };
    delete holds[name];
    stickSel = holds;
    try {
      await saveCustomList(customLabels.filter((c) => c.name !== name));
    } catch (e) {
      saveErr = e?.message ?? 'Could not remove task type';
    } finally {
      removing = null;
    }
  }

  // ---- Section B · provider-module catalog + Section C · Fireworks sync ----------------------
  // Both are org-wide reads, independent of the scope selector. The sync endpoint is always-200:
  // key-missing and upstream failure arrive as {key_present, error} data, never as query errors.
  // OSS reads the EFFECTIVE catalog (base + the local scope's adoptions, same row shape) so an
  // adopted model shows up here, not only in the binding dropdowns; enterprise keeps the global
  // shipped-catalog view (effective-models needs a named scope the operator may not have).
  const catQ = query(() => (OSS ? getEffectiveModels() : getCatalogModels()),
                     { isEmpty: (d) => !d?.models?.length });
  const syncQ = query(() => getFireworksSync(), { isEmpty: () => false });
  const providerGroups = $derived(groupByProvider(catQ.data?.models ?? []));
  const sync = $derived(syncQ.data);
  const syncWarns = $derived(driftCounts(sync?.drift).warn);
  const syncRows = $derived((sync?.ok ?? []).length + (sync?.drift ?? []).length);
  const FW = { hue: providerHue('fireworks'), mark: providerMark('fireworks') };

  // Expandable per-model advanced detail (upstream ref, endpoint wiring, source fragment).
  let expanded = $state(new Set());
  function toggleDetail(id) {
    const next = new Set(expanded);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    expanded = next;
  }

  // Adopt / Fix ref → YAML modal with copy-to-clipboard.
  let yamlOpen = $state(false);
  let yamlModal = $state(null); // {label, title, yaml, instruction} from driftAction()
  let copied = $state(false);
  function openYaml(action) {
    yamlModal = action;
    copied = false;
    yamlOpen = true;
  }
  async function copyYaml() {
    try {
      await navigator.clipboard.writeText(yamlModal?.yaml ?? '');
      copied = true;
      setTimeout(() => (copied = false), 1400);
    } catch {
      /* clipboard blocked — the block is selectable by hand */
    }
  }

  // The model LIBRARY (discovery panels) moved to /models — this page keeps the curated
  // catalog view; the Fireworks sync panel's Adopt/Fix ref still uses the YAML modal above.

  // ---- Remove an adopted model (Section B rows with source === 'adopted') --------------------
  // Base catalog models are YAML-owned and not removable here; adoptions are DB rows with a
  // DELETE endpoint. The confirm modal names every task type pointing at the model and reverts
  // them on confirm — a dangling binding would silently fall back at dispatch, but leaving it
  // stored is exactly the stale-binding mess the routing view has to flag.
  let removeAdoptedOpen = $state(false);
  let removeTarget = $state(null); // the section-B catalog row being removed
  let removingAdopted = $state(false);
  let removeAdoptedErr = $state(null);

  // Task types currently pointing at `id` in the EDITED view (what the user sees on this page).
  function bindingsOn(id) {
    const builtins = (routingQ.data?.labels ?? []).filter(
      (r) => !r.custom && r.bindable && (routeSel[r.label] ?? r.model) === id);
    const customs = customLabels.filter((c) => (routeSel[c.name] ?? c.model) === id);
    return { builtins, customs };
  }
  function openRemoveAdopted(m) {
    removeTarget = m;
    removeAdoptedErr = null;
    removeAdoptedOpen = true;
  }
  const removeAffected = $derived(
    removeTarget ? bindingsOn(removeTarget.id) : { builtins: [], customs: [] });
  // Where a custom type lands when its model is removed: the Generalist's (post-revert) pick —
  // the designated catch-all. A custom type can't go unbound (the policy PUT requires a model).
  const removeFallback = $derived.by(() => {
    if (!removeTarget) return null;
    const other = (routingQ.data?.labels ?? []).find((r) => r.label === 'other');
    const cur = routeSel['other'] ?? other?.model;
    return cur === removeTarget.id ? other?.default_model : cur;
  });
  // ---- Advanced controls disclosure ("When routing degrades") --------------------------------
  // Collapsed by default so the routing table leads the page; the open state sticks per browser.
  let degradedOpen = $state(browser && localStorage.getItem('toto.catalog.advanced') === '1');
  function toggleDegraded() {
    degradedOpen = !degradedOpen;
    if (browser) localStorage.setItem('toto.catalog.advanced', degradedOpen ? '1' : '0');
  }

  // ---- Add local model — self-contained flow (AddLocalModel.svelte), mounted here today and
  // in Settings later; this page only opens it and reloads its catalog views on success.
  let lmOpen = $state(false);

  async function confirmRemoveAdopted() {
    removingAdopted = true;
    removeAdoptedErr = null;
    const id = removeTarget.id;
    try {
      const { builtins, customs } = bindingsOn(id);
      const classifierHit = classifierModel === id;
      if (builtins.length || customs.length || classifierHit) {
        const sel = { ...routeSel };
        for (const r of builtins) sel[r.label] = r.default_model;
        const fb = removeFallback;
        for (const c of customs) sel[c.name] = fb;
        routeSel = sel;
        if (classifierHit) classifierModel = '';
        await saveCustomList(customLabels.map((c) =>
          customs.some((x) => x.name === c.name) ? { ...c, model: fb } : c));
      }
      await deleteAdoption(id);
      await Promise.all([modelsQ.reload(), catQ.reload()]);
      removeAdoptedOpen = false;
    } catch (e) {
      removeAdoptedErr = e?.message ?? 'Could not remove the model';
    } finally {
      removingAdopted = false;
    }
  }
</script>

<svelte:head><title>Catalog &amp; Routing · Toto Control</title></svelte:head>

<div class="pagehead">
  <div>
    <h1>Catalog &amp; Routing</h1>
    <div class="sub">
      Which models are available, and which model each task type auto-selects. Denials fail closed
      at dispatch.
    </div>
  </div>
  <div class="right">
    {#if OSS}
      <span class="scopepill">Routing policy</span>
    {:else}
      <span class="scopepill">Policy for <b>{teamName}</b><span class="chev">▾</span></span>
    {/if}
    <button class="btn small primary" disabled={saving || !scopeReady || !ready(routingQ) || !ready(modelsQ)} onclick={save}>
      {saving ? 'Saving…' : `Save policy · v${version}`}
    </button>
  </div>
</div>

{#if saveErr}
  <div class="reltip" style="border-color:var(--crit);background:var(--crit-soft)" in:revealIn>
    <svg viewBox="0 0 24 24"><path d="M12 8v5M12 16h.01" /><circle cx="12" cy="12" r="9" /></svg>
    <div><b>Save failed.</b> {saveErr}</div>
  </div>
{/if}

{#if !OSS && teamsQ.status === 'unauthed'}
  {@render deadend('Sign in required', 'Your session has expired. Sign in to manage routing.')}
{:else if !OSS && teamsQ.status === 'forbidden'}
  {@render deadend('Admin access needed', 'You need an admin or owner role to configure Catalog & Routing.')}
{:else}
  <!-- Routing (A) and team access (D) need the teams read; the catalog (B) and Fireworks sync (C)
       are global and render regardless — an operator without ?org_id= still gets B/C. -->
  {#if !OSS && teamsQ.status === 'loading'}
    <SkeletonTable rows={6} cols={3} />
  {:else if !OSS && needsOrg}
    <div class="stub" style="margin-bottom:24px" in:revealIn>
      <div class="ic"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></svg></div>
      <b>Pick an organization</b>
      <p>
        The operator credential has no home org — name the org whose routing you want to manage.
        The model catalog and Fireworks sync below are global and don’t need one.
      </p>
      <form class="orgform" onsubmit={(e) => { e.preventDefault(); submitOrg(); }}>
        <input placeholder="org id" bind:value={orgDraft} aria-label="Organization id" />
        <button class="btn small primary" type="submit">View</button>
      </form>
    </div>
  {:else if !OSS && teamsQ.status === 'error'}
    {@render deadend('Could not load teams', teamsQ.error?.message ?? 'Unknown error')}
  {:else}
  {#if !OSS}
    <!-- Scope context: the team switcher needs a home; OSS is single-tenant and the page header
         already says everything this band would. -->
    <div class="teamband">
      <div class="teamemblem">{emblem}</div>
      <div class="teamlead">
        <div class="teamswitch">
          <select class="teambtn teamsel" bind:value={teamId} aria-label="Team">
            <option value={ORG}>Organization (default)</option>
            {#each teams as t}<option value={t.team_id}>{t.name}</option>{/each}
          </select>
        </div>
        <div class="teamframe">
          <b>Routing — {teamName}</b>
          <span>
            {#if isOrg}
              Default model choices for owner and API-token traffic without a team.
            {:else}
              Which model each task type uses for the {teamName} team.
            {/if}
          </span>
        </div>
      </div>
      <div class="teamright">
        <span class="chip"><span class="d" style="background:var(--accent-2)"></span>{teamName}</span>
      </div>
    </div>
  {/if}

  <!-- ===== TASK ROUTING — the core of the page ===== -->
  <div class="secthead core">
    <h2>Task routing</h2>
    <span class="hint">
      Every request{OSS ? '' : ` in ${teamName}`} is classified into a task type; each type
      auto-selects one catalog model. Pick a model, or leave it on the clear default.
    </span>
  </div>

  <div class="card" style="margin-bottom:24px">
    <div class="ch">
      <h3>Task type → model</h3>
      <span class="meta">{teamName} · policy v{version} · {bindableCount} bindable</span>
    </div>
    <div class="optbar">
      <span class="lab">Optimize</span>
      <SegmentedControl options={OPT_OPTS} bind:value={optimize} />
      <span class="exp">{optHint}</span>
      <button class="btn small" style="margin-left:auto" onclick={openAdd} disabled={!ready(modelsQ)}>
        + Add task type
      </button>
    </div>
    {#if routingQ.status === 'loading'}
      <div style="padding:15px"><SkeletonTable rows={6} cols={3} title={false} /></div>
    {:else if routingQ.status === 'forbidden' || routingQ.status === 'unauthed'}
      {@render deadend('Routing policy locked', 'You lack permission to view this team’s routing policy.')}
    {:else if routingQ.status === 'error'}
      {@render deadend('Could not load routing', routingQ.error?.message ?? 'Unknown error')}
    {:else}
      <!-- Full height, no inner scroll well — the routing table is the core of the page. -->
      <div class="tablewrap" in:revealIn>
        <table class="tasktable">
          <thead>
            <tr><th style="width:34%">Task type</th><th>Auto-selects model</th><th title="After a conversation's first request is routed, follow-up turns in that conversation reuse the same model for this long before re-routing — keeps answers consistent and provider caches warm. Default uses the gateway-wide hold (15 minutes unless configured).">Hold&thinsp;<span class="holdq" aria-hidden="true">?</span></th></tr>
          </thead>
          <tbody>
            {#each [...routingQ.data.labels.filter((r) => r.label === 'other'), ...routingQ.data.labels.filter((r) => r.label !== 'other')] as row}
              <tr class:governed={!row.bindable} class:genrow={row.label === 'other'}>
                <td>
                  <div class="tasktype">
                    <span class="troute" aria-hidden="true"></span>{taskLabel(row.label)}
                    {#if row.label === 'other'}<span class="chip gen" style="margin-left:2px">catch-all</span>{/if}
                    {#if row.custom}<span class="chip ovr" style="margin-left:2px">custom</span>{/if}
                  </div>
                  {#if row.label === 'other'}
                    <div class="taskdesc">Requests that don't match a task type — or can't be classified — land here.</div>
                  {:else if row.desc}<div class="taskdesc">{row.desc}</div>{/if}
                  {#if row.custom}
                    <div class="ctacts">
                      <button class="btn small ghost" title="Edit this task type’s description or model"
                        onclick={() => openEdit(row.label)}>Edit</button>
                      <button
                        class="btn small ghost danger"
                        class:armed={removeArm === row.label}
                        disabled={removing === row.label}
                        title="Remove this task type"
                        onblur={() => removeArm === row.label && (removeArm = null)}
                        onclick={() => removeCustom(row.label)}
                      >
                        {removing === row.label ? 'Removing…' : removeArm === row.label ? 'Really remove?' : 'Remove'}
                      </button>
                    </div>
                  {/if}
                </td>
                {#if row.bindable}
                  <td>
                    <div class="flowcell">
                      <span class="flowarrow" aria-hidden="true"></span>
                      <ModelPicker
                        models={modelList}
                        value={routeSel[row.label] ?? row.model}
                        {allowed}
                        defaultId={row.custom ? null : row.default_model}
                        taskLabel={row.label}
                        {traffic}
                        newer={newerSet}
                        onviewcatalog={scrollToCatalog}
                        ariaLabel="Model for {row.label}"
                        onchange={(id) => pickModel(row.label, id)}
                      />
                      {#if !row.custom && isOver(row) && allowed.has(row.default_model)}
                        <button class="resetdf" title="Reset to default ({prettyId(row.default_model)})" onclick={() => resetRow(row)}>↺</button>
                      {/if}
                    </div>
                  </td>
                  <td>
                    <select
                      class="routesel"
                      aria-label="Hold for {row.label}"
                      value={stickSel[row.label] ?? 0}
                      onchange={(e) => pickHold(row.label, Number(e.currentTarget.value))}
                    >
                      {#each HOLD_OPTS as [secs, opt]}
                        <option value={secs}>{opt}</option>
                      {/each}
                    </select>
                  </td>
                {:else}
                  <td colspan="2">
                    <span class="lockpill">
                      <svg viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="9" rx="2" /><path d="M8 11V8a4 4 0 0 1 8 0v3" /></svg>
                      routed by {row.label === 'redact' ? 'privacy guard' : 'fallback path'}
                    </span>
                  </td>
                {/if}
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>

  {#if isOrg}
    <!-- Advanced controls, out of the routing table's way: failure modes, the classifier,
         and data classification. Collapsed by default; open state sticks per browser. -->
    <div class="card collapsible degradedcard" class:collapsed={!degradedOpen} style="margin-bottom:24px">
      <div
        class="ch"
        role="button"
        tabindex="0"
        aria-expanded={degradedOpen}
        onclick={toggleDegraded}
        onkeydown={(e) => (e.key === ' ' || e.key === 'Enter') && (e.preventDefault(), toggleDegraded())}
      >
        <h3>When routing degrades</h3>
        <span class="meta">Failure handling · which model reads your prompts · data-sensitivity rules</span>
        <span class="caret" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></svg></span>
      </div>
      <div class="cb">
        <div class="failbox">
          <div class="failhead">
            <span class="lab">When smart routing is unavailable</span>
            <span class="exp">Choose per failure reason whether to keep serving (fall back to a default
              model) or reject the request with an error.</span>
          </div>
          {#each FAIL_REASONS as r (r.key)}
            <div class="failrow">
              <div class="failwhat">
                <b>{r.label}</b>
                <span class="exp">{r.hint}</span>
              </div>
              <SegmentedControl options={FAIL_OPTS} bind:value={failMatrix[r.key]} />
            </div>
          {/each}
        </div>

        <!-- Pluggable classifier: which model reads each prompt to route it. -->
        <div class="optbar">
          <span class="lab">Which model reads your prompts to route them</span>
          <ModelPicker
            models={modelList}
            value={classifierModel}
            {traffic}
            newer={newerSet}
            noneLabel="Default (platform classifier)"
            onviewcatalog={scrollToCatalog}
            ariaLabel="Classifier model"
            onchange={(id) => (classifierModel = id)}
          />
          <span class="exp">
            {#if classifierLeaks}
              <strong class="warn">This model runs in the cloud. Your data-classification rules require
                the classifier to stay in-perimeter — pick an in-perimeter model or Save will be rejected.</strong>
            {:else}
              It reads every prompt before routing, so a data-restricted org should keep it in-perimeter.
            {/if}
          </span>
        </div>

        <!-- Data classification: org-defined sensitivity labels bound to residency constraints. -->
        <div class="taxbox">
          <div class="taxhead">
            <span class="lab">Data classification</span>
            <span class="exp">Every request is also classified for data sensitivity; the constraint
              here holds even when a client names a model directly.</span>
            <button class="btn small" style="margin-left:auto" onclick={addTaxRow}>+ Add label</button>
          </div>
          {#if taxLabels.length === 0}
            <div class="taxempty">No data-classification labels. Requests route without a data-policy constraint.</div>
          {:else}
            <div class="taxrows">
              {#each taxLabels as row, i (i)}
                <div class="taxrow">
                  <input class="taxname" placeholder="label (e.g. restricted)" bind:value={row.name} />
                  <input class="taxdesc" placeholder="what this covers, in plain language" bind:value={row.desc} />
                  <select class="taxsel" bind:value={row.constraint}>
                    {#each CONSTRAINT_OPTS as [val, label]}
                      <option value={val}>{label}</option>
                    {/each}
                  </select>
                  <button class="btn small ghost" title="Remove" onclick={() => removeTaxRow(i)}>✕</button>
                </div>
              {/each}
            </div>
            <div class="taxdefault">
              <span class="lab">If a request can’t be classified</span>
              <select bind:value={taxDefault}>
                <option value="">No constraint</option>
                {#each taxNames as name}
                  <option value={name}>Apply “{name}”</option>
                {/each}
              </select>
            </div>
          {/if}
        </div>
      </div>
    </div>
  {/if}
  {/if}

  <!-- ===== MODEL CATALOG (provider modules) ===== -->
  <div class="secthead">
    <h2>Model catalog</h2>
    <span class="hint">
      Every model this gateway can serve, grouped by provider. Expand a row for the wiring
      details — adopt more in the <a href="/models">Model Library</a>.
    </span>
    <button class="btn small" onclick={() => (lmOpen = true)}>+ Add local model</button>
  </div>

  {#if catQ.status === 'loading'}
    <SkeletonTable rows={5} cols={5} />
  {:else if catQ.status === 'unauthed' || catQ.status === 'forbidden'}
    {@render deadend('Catalog locked', 'You need an admin or owner role to view the full model catalog.')}
  {:else if catQ.status === 'error'}
    {@render deadend('Could not load the catalog', catQ.error?.message ?? 'Unknown error')}
  {:else if catQ.status === 'empty'}
    {@render deadend('No models in the catalog', 'This gateway is running without any catalog fragments.')}
  {:else}
    {#each providerGroups as g (g.provider)}
      <div class="card provcard" style="--ph:{g.hue}" in:revealIn>
        <div class="ch provhead">
          <span class="pmark" aria-hidden="true">
            {#if logoFor(g.provider)}{@html logoFor(g.provider)}{:else}{g.mark}{/if}
          </span>
          <h3>{g.label}</h3>
          <span class="pfacts">
            <span>{g.models.length} model{g.models.length === 1 ? '' : 's'}</span>
            {#if g.provider === 'openrouter' || g.provider === 'cloudflare'}<span class="pfact">aggregator — many labs, one key</span>{/if}
            {#if g.provider === 'fake'}<span class="pfact">built-in echo models for verifying your gateway without spending</span>{/if}
            {#if g.provider === 'local'}<span class="pfact">OpenAI-compatible servers on your machine — nothing leaves your network</span>{/if}
            {#if g.keyEnv}<span class="pfact n" title="The gateway reads this env var for the provider key">key: {g.keyEnv}</span>{/if}
            {#if g.fineTuned}<span class="pfact tuned">{g.fineTuned} fine-tuned</span>{/if}
          </span>
        </div>
        <div class="tablewrap">
          <table class="provtable">
            <thead>
              <tr>
                <th>Model</th><th class="r">$ / 1M in·out</th>
                <th class="r">Context</th><th>Residency</th><th class="dcol"></th>
              </tr>
            </thead>
            <tbody>
              {#each g.models as m (m.id)}
                <tr id="cat-{m.id}">
                  <td>
                    <div class="mname">
                      {displayName(m)}
                      {#if m.fine_tuned}<span class="ftbadge">Fine-tuned · yours</span>{/if}
                      {#if m.source === 'adopted'}<span class="ftbadge">Added by you</span>{/if}
                    </div>
                    <div class="malias">
                      <span class="idchip n">{m.id}</span>
                      {#each m.aliases ?? [] as a}<span class="idchip n alias" title="alias">{a}</span>{/each}
                    </div>
                  </td>
                  <td class="r n">{mtok(m.price_in)} · {mtok(m.price_out)}</td>
                  <td class="r n">{ctxShort(m.context_window)}</td>
                  <td>
                    {#if m.residency_class === 'in_perimeter'}
                      <span class="chip perim"><span class="d"></span>in-perimeter</span>
                    {:else}
                      <span class="chip cloud"><span class="d"></span>cloud</span>
                    {/if}
                  </td>
                  <td class="dcol">
                    {#if m.source === 'adopted'}
                      <button
                        class="disclose rmadopt"
                        title="Remove from catalog"
                        aria-label="Remove {m.id} from catalog"
                        onclick={() => openRemoveAdopted(m)}
                      >
                        <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
                      </button>
                    {/if}
                    <button
                      class="disclose"
                      class:open={expanded.has(m.id)}
                      aria-expanded={expanded.has(m.id)}
                      aria-label="Details for {m.id}"
                      title="Wiring details"
                      onclick={() => toggleDetail(m.id)}
                    >
                      <svg viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></svg>
                    </button>
                  </td>
                </tr>
                {#if expanded.has(m.id)}
                  {@const up = upstreamParts(m.upstream_model)}
                  <tr class="drow">
                    <td colspan="5">
                      <div class="dgrid">
                        <div class="df">
                          <span>Upstream</span>
                          <b class="n" title={m.upstream_model}>
                            {up.base || '—'}{#if up.dep}&nbsp;<span class="depchip">#{up.dep}</span>{/if}
                          </b>
                        </div>
                        {#if servingLabel(m)}<div class="df"><span>Serving</span><b>{servingLabel(m)}</b></div>{/if}
                        <div class="df"><span>Endpoint</span><b class="n">{m.endpoint === 'fake' ? 'built-in echo' : (m.endpoint ?? '—')}</b></div>
                        <div class="df"><span>Base URL</span><b class="n" title={m.base_url}>{m.base_url ?? '—'}</b></div>
                        <!-- api_key_env is only real on openai-shaped endpoints; elsewhere it's a schema default -->
                        {#if m.endpoint === 'openai'}
                          <div class="df"><span>API key env</span><b class="n">{m.api_key_env ?? '—'}</b></div>
                        {/if}
                        <div class="df"><span>Tools</span><b>{m.tools ? 'yes' : 'no'}</b></div>
                        <div class="df"><span>Source</span><b class="n">{m.source ?? '—'}</b></div>
                      </div>
                    </td>
                  </tr>
                {/if}
              {/each}
            </tbody>
          </table>
        </div>
      </div>

    {/each}
  {/if}

  <!-- ===== FIREWORKS SYNC ===== -->
  <div class="secthead">
    <h2>Fireworks sync</h2>
    <span class="hint">
      Models you fine-tune in Fireworks, checked live against this catalog — adopt new ones, catch
      stale references.
    </span>
  </div>

  <div class="card synccard" style="--ph:{FW.hue};margin-bottom:24px">
    <div class="ch provhead">
      <span class="pmark" aria-hidden="true">
        {#if logoFor('fireworks')}{@html logoFor('fireworks')}{:else}{FW.mark}{/if}
      </span>
      <h3>Fireworks ⇄ catalog</h3>
      <span class="meta">
        {#if sync?.account}{sync.account}{syncFreshness(sync) ? ` · ${syncFreshness(sync)}` : ''}{:else}{syncFreshness(sync)}{/if}
      </span>
      <button class="btn small" onclick={() => syncQ.reload()} disabled={syncQ.status === 'loading'}>
        {syncQ.status === 'loading' ? 'Checking…' : 'Refresh'}
      </button>
    </div>
    {#if syncQ.status === 'loading'}
      <div class="cb"><SkeletonTable rows={3} cols={2} title={false} /></div>
    {:else if syncQ.status === 'unauthed' || syncQ.status === 'forbidden'}
      <div class="cb synchint"><b>Admin access needed</b><p>Your role can’t view the Fireworks sync state.</p></div>
    {:else if syncQ.status === 'error'}
      <div class="cb synchint">
        <b>Couldn’t check Fireworks</b>
        <p>{syncQ.error?.message ?? 'Unknown error'} — hit Refresh to retry.</p>
      </div>
    {:else if sync && !sync.key_present}
      <div class="cb synchint">
        <b>Connect Fireworks</b>
        <p>
          Set <span class="n">FIREWORKS_API_KEY</span> on the gateway to enable sync — models you
          fine-tune in your Fireworks account will show up here, ready to adopt into the catalog.
        </p>
      </div>
    {:else if sync?.error}
      <div class="cb">
        <div class="reltip" style="border-color:var(--warn);background:var(--warn-soft)">
          <svg viewBox="0 0 24 24"><path d="M12 8v5M12 16h.01" /><circle cx="12" cy="12" r="9" /></svg>
          <div>
            <b>Fireworks didn’t answer.</b>
            {sync.error} — the catalog itself is unaffected; hit Refresh to retry.
          </div>
        </div>
      </div>
    {:else if sync}
      <div class="cb syncbody">
        <div class="syncsum">
          <span class="state {syncWarns ? 'warn' : 'ok'}"><span class="d"></span>{driftSummary(sync.drift)}</span>
          <span class="syncfacts n">
            {(sync.account_models ?? []).length} account model{(sync.account_models ?? []).length === 1 ? '' : 's'}
            · {(sync.deployments ?? []).length} deployment{(sync.deployments ?? []).length === 1 ? '' : 's'}
            · {(sync.catalog_entries ?? []).length} cataloged
          </span>
        </div>
        {#if !syncRows}
          <p class="syncempty">
            No fine-tuned models in this Fireworks account yet — when a tuning job completes, it
            appears here.
          </p>
        {:else}
          <div class="syncrows">
            {#each sync.ok ?? [] as r (r.catalog_id)}
              <div class="srow">
                <span class="state ok" title="Catalog and Fireworks agree"><span class="d"></span></span>
                <span class="stext">
                  <b>{r.catalog_id}</b> ↔ <span class="n">#{lastSeg(r.deployment)}</span>{r.deployment_state ? ` · ${r.deployment_state}` : ''}
                </span>
              </div>
            {/each}
            {#each sync.drift ?? [] as d, i (i)}
              {@const action = driftAction(d)}
              <div class="srow">
                <span class="state {d.severity === 'warn' ? 'warn' : 'info'}"><span class="d"></span></span>
                <span class="stext">{driftSentence(d)}</span>
                {#if action}
                  <button class="btn small actbtn" onclick={() => openYaml(action)}>{action.label}</button>
                {/if}
              </div>
            {/each}
          </div>
        {/if}
      </div>
    {/if}
  </div>

  {#if !isOrg && ready(teamsQ)}
    <!-- ===== SECTION D · TEAM ACCESS ===== -->
    <div class="secthead">
      <h2>Team access</h2>
      <span class="hint">
        Which catalog models the {teamName} team is allowed to use at all — this governs availability
        for every task type above.
      </span>
    </div>

    <div class="reltip">
      <svg viewBox="0 0 24 24"><path d="M12 8v5M12 16h.01" /><circle cx="12" cy="12" r="9" /></svg>
      <div>
        <b>Access governs availability; routing governs selection.</b> Deny a model here and it drops out
        of every task-routing dropdown above — any task type that defaulted to it is auto-overridden to
        an allowed sibling.
      </div>
    </div>

    <!-- plain-language switches -->
    <div class="policybar">
      <div class="pswitch">
        <Toggle bind:checked={resLocal} label="Allow local models" />
        <span class="t">Allow local models<small>in-perimeter, never leaves your network</small></span>
      </div>
      <div class="pswitch">
        <Toggle bind:checked={resCloud} label="Allow frontier / cloud" />
        <span class="t">Allow frontier / cloud<small>routes to external providers</small></span>
      </div>
      <div class="provlist">
        <span class="lab">Providers</span>
        {#each providerVias as prov}
          <span
            class="prov"
            class:on={providersOn.has(prov)}
            class:off={!providersOn.has(prov)}
            role="button"
            tabindex="0"
            onclick={() => toggleProvider(prov)}
            onkeydown={(e) => (e.key === ' ' || e.key === 'Enter') && (e.preventDefault(), toggleProvider(prov))}
          >{prov}</span>
        {/each}
      </div>
    </div>

    <div class="card collapsible" class:collapsed={catalogCollapsed}>
      <div
        class="ch"
        role="button"
        tabindex="0"
        aria-expanded={!catalogCollapsed}
        onclick={() => (catalogCollapsed = !catalogCollapsed)}
        onkeydown={(e) => (e.key === ' ' || e.key === 'Enter') && (e.preventDefault(), (catalogCollapsed = !catalogCollapsed))}
      >
        <h3>Per-model access</h3>
        <span class="meta">{allModelIds.length} models · {allowedCount} allowed for {teamName}</span>
        <span class="caret" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></svg></span>
      </div>
      {#if modelsQ.status === 'loading'}
        <div class="cb"><SkeletonTable rows={6} cols={6} title={false} /></div>
      {:else if modelsQ.status === 'error'}
        {@render deadend('Could not load model catalog', modelsQ.error?.message ?? 'Unknown error')}
      {:else}
        <div class="tablewrap scrollist">
          <table>
            <thead>
              <tr>
                <th>Model</th><th>Provider</th>
                <th
                  class="r sortable"
                  class:sorted={priceSort}
                  role="button"
                  tabindex="0"
                  aria-sort={priceSort === 'asc' ? 'ascending' : priceSort === 'desc' ? 'descending' : 'none'}
                  title="Sort by price"
                  onclick={cyclePriceSort}
                  onkeydown={(e) => (e.key === ' ' || e.key === 'Enter') && (e.preventDefault(), cyclePriceSort())}
                >$ / 1M in·out <span class="sortcaret">{priceSort === 'asc' ? '▲' : priceSort === 'desc' ? '▼' : '↕'}</span></th>
                <th class="r">Context</th><th>Residency</th>
                <th style="text-align:center">Access</th><th style="text-align:center">Default</th>
              </tr>
            </thead>
            <tbody>
              {#each sortedModels as m}
                {@const perim = m.residency_class === 'in_perimeter'}
                <tr class:denied={!allowed.has(m.id)}>
                  <td>
                    <div class="mname">{prettyModel(m)}</div>
                    <div class="malias">{m.id}</div>
                  </td>
                  <td><span class="provbadge">{providerLabel(m.provider ?? m.via)}</span></td>
                  <td class="r n" class:muted={!m.price_in && !m.price_out}>
                    {#if priceTier(m)}<span class="pricedot" data-tier={priceTier(m)} title="{priceTier(m)} price"></span>{/if}{mtok(m.price_in)} · {mtok(m.price_out)}
                  </td>
                  <td class="r n">{ctxFmt(m.context_window)}</td>
                  <td>
                    <span class="chip" class:perim class:cloud={!perim}><span class="d"></span>{perim ? 'in-perimeter' : 'cloud'}</span>
                  </td>
                  <td class="actcell" style="text-align:center">
                    <Toggle checked={allowed.has(m.id)} label="Allow {m.id}" onchange={() => toggleAllow(m.id)} />
                  </td>
                  <td class="actcell" style="text-align:center">
                    <span
                      class="star"
                      class:on={defaultModel === m.id}
                      role="button"
                      tabindex="0"
                      title={defaultModel === m.id ? 'Team default' : 'Set as team default'}
                      onclick={() => setDefault(m.id)}
                      onkeydown={(e) => (e.key === ' ' || e.key === 'Enter') && (e.preventDefault(), setDefault(m.id))}
                    >
                      <svg viewBox="0 0 24 24"><path d="M12 3l2.6 5.6 6.1.7-4.5 4.2 1.2 6-5.4-3-5.4 3 1.2-6L3.3 9.3l6.1-.7z" /></svg>
                    </span>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </div>
  {/if}
{/if}

<!-- ===== Add / edit custom task type (CT) ===== -->
<Modal
  bind:open={addOpen}
  title={ctEditing ? 'Edit task type' : 'Add task type'}
  subtitle={ctEditing
    ? `Change what “${ctEditing}” covers or which model it uses.`
    : OSS
      ? 'Define a custom task type for this gateway.'
      : isOrg
        ? 'Define a custom task type for this organization.'
        : `Define a custom task type for the ${teamName} team.`}
>
  <div class="field">
    <label for="ct-name">Name (slug)</label>
    <input id="ct-name" bind:value={ctName} placeholder="invoice_parsing" spellcheck="false" disabled={!!ctEditing} />
    {#if ctErr?.field === 'name'}<div class="fielderr">{ctErr.message}</div>{/if}
    {#if ctEditing}<div class="fieldnote">The name is this task type’s identity — remove and re-add to rename.</div>{/if}
  </div>
  <div class="field">
    <label for="ct-desc">Description</label>
    <input id="ct-desc" bind:value={ctDesc} placeholder="extract line items and totals from an invoice" />
    {#if ctErr?.field === 'desc'}<div class="fielderr">{ctErr.message}</div>{/if}
    {#if ctThin}
      <div class="descwarn">Probably too thin for the classifier to match reliably — aim for one full sentence.</div>
    {/if}
    <div class="fieldnote">
      The classifier matches every incoming prompt against this line — it <b>is</b> the routing
      behavior. Describe the request (what the user asks for) in one focused sentence, concrete and
      distinct from the existing types: overlap a built-in and you steal its traffic.
    </div>
    <div class="descpair">
      <span class="good">✓ writing or explaining SQL queries against a relational database</span>
      <span class="bad">✗ database stuff</span>
    </div>
    {#if ctExamples.length}
      <details class="exemplars">
        <summary>How the built-ins describe themselves</summary>
        <ul>
          {#each ctExamples as r (r.label)}
            <li><span class="n">{r.label}</span> — {r.desc}</li>
          {/each}
        </ul>
      </details>
    {/if}
  </div>
  <div class="field">
    <label for="ct-model">Bound model</label>
    <ModelPicker
      models={modelList}
      value={ctModel}
      {allowed}
      taskLabel={ctEditing ?? (ctName.trim() || null)}
      {traffic}
      newer={newerSet}
      ariaLabel="Bound model"
      onchange={(id) => (ctModel = id)}
    />
    {#if ctErr?.field === 'model'}<div class="fielderr">{ctErr.message}</div>{/if}
  </div>
  {#if ctErr && !ctErr.field}<div class="fielderr">{ctErr.message}</div>{/if}
  {#snippet footer()}
    <button class="btn ghost" onclick={() => (addOpen = false)}>Cancel</button>
    <button class="btn primary" disabled={ctSaving || !ctName.trim() || !ctDesc.trim() || !ctModel} onclick={submitCustom}>
      {ctSaving ? 'Saving…' : ctEditing ? 'Save changes' : 'Add task type'}
    </button>
  {/snippet}
</Modal>

<AddLocalModel bind:open={lmOpen} onadded={() => Promise.all([catQ.reload(), modelsQ.reload()])} />

<!-- ===== Remove adopted model (Section B) ===== -->
<Modal
  bind:open={removeAdoptedOpen}
  title="Remove from catalog"
  subtitle={removeTarget
    ? `“${displayName(removeTarget)}” (${removeTarget.id}) was added from the ${providerLabel(removeTarget.provider)} library.`
    : ''}
>
  {#if removeAffected.builtins.length || removeAffected.customs.length}
    <div class="rmwarn">
      <b>Task types currently using this model</b>
      <ul>
        {#each removeAffected.builtins as r (r.label)}
          <li><span class="n">{taskLabel(r.label)}</span> — reverts to its default, {prettyId(r.default_model)}</li>
        {/each}
        {#each removeAffected.customs as c (c.name)}
          <li><span class="n">{c.name}</span> (custom) — will use {prettyId(removeFallback)}</li>
        {/each}
      </ul>
    </div>
  {:else}
    <p class="rmnote">No task types point at this model. It disappears from the catalog and every model picker.</p>
  {/if}
  {#if removeTarget && classifierModel === removeTarget.id}
    <p class="rmnote">This model also reads your prompts to route them — that resets to the platform default.</p>
  {/if}
  {#if removeAdoptedErr}<div class="fielderr">{removeAdoptedErr}</div>{/if}
  {#snippet footer()}
    <button class="btn ghost" onclick={() => (removeAdoptedOpen = false)}>Cancel</button>
    <button class="btn primary" disabled={removingAdopted} onclick={confirmRemoveAdopted}>
      {removingAdopted ? 'Removing…' : 'Remove model'}
    </button>
  {/snippet}
</Modal>

<!-- ===== Adopt / Fix-ref YAML modal (Fireworks sync drift rows) ===== -->
<Modal bind:open={yamlOpen} title={yamlModal?.title ?? ''} subtitle={yamlModal?.instruction ?? ''}>
  <pre class="yamlblock n">{yamlModal?.yaml ?? ''}</pre>
  {#snippet footer()}
    <button class="btn ghost" onclick={() => (yamlOpen = false)}>Close</button>
    <button class="btn primary" onclick={copyYaml}>{copied ? 'Copied ✓' : 'Copy YAML'}</button>
  {/snippet}
</Modal>

{#snippet deadend(title, msg)}
  <div class="stub" in:revealIn>
    <div class="ic"><svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h10" /><circle cx="18" cy="18" r="2.4" /></svg></div>
    <b>{title}</b>
    <p>{msg}</p>
  </div>
{/snippet}

<style>
  /* W2-C7 data-classification editor (org scope). */
  .taxbox {
    margin: 4px 0 2px;
    padding: 12px 14px;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: var(--surface-2, transparent);
  }
  /* Per-reason fail-policy matrix (W2-C7) — same box family as .taxbox. */
  .failbox {
    margin: 4px 0 2px; padding: 12px 14px;
    border: 1px solid var(--line); border-radius: 10px; background: var(--surface-2, transparent);
  }
  .failhead { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; margin-bottom: 4px; }
  .failrow { display: flex; align-items: center; gap: 12px; padding: 8px 0; border-top: 1px solid var(--line); }
  .failwhat { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .failwhat b { font-size: 0.8125rem; font-weight: calc(600 + (var(--ui-weight) - 400)); }
  .taxhead { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
  .taxempty { margin-top: 8px; font-size: 0.8125rem; color: var(--muted); }
  .taxrows { margin-top: 10px; display: flex; flex-direction: column; gap: 8px; }
  .taxrow { display: flex; align-items: center; gap: 8px; }
  .taxrow .taxname { flex: 0 0 140px; }
  .taxrow .taxdesc { flex: 1 1 auto; }
  .taxrow input, .taxrow select, .taxdefault select {
    padding: 6px 8px;
    border: 1px solid var(--line);
    border-radius: 7px;
    background: var(--surface, transparent);
    color: inherit;
    font-size: 0.8125rem;
  }
  .taxsel { flex: 0 0 auto; }
  /* Hold header affordance: signals the tooltip carrying the honest stickiness explanation. */
  .holdq {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 13px;
    height: 13px;
    border-radius: 50%;
    border: 1px solid var(--line-2);
    font-size: 0.5625rem;
    color: var(--text-3);
    cursor: help;
    vertical-align: 1px;
  }
  .exp .warn { color: var(--danger, #c0392b); font-weight: 600; }
  .taxdefault { margin-top: 10px; display: flex; align-items: center; gap: 10px; }
  .btn.ghost { background: transparent; }

  /* The Generalist (catch-all `other` binding) is the org's single most consequential routing
     choice — it takes everything unclassified AND classifier failures. Featured row treatment. */
  :global(tr.genrow td) { background: color-mix(in oklab, var(--accent-soft) 55%, transparent); }
  :global(tr.genrow) { border-left: 3px solid var(--accent); }
  :global(tr.genrow .tasktype) { font-weight: calc(640 + (var(--ui-weight) - 400)); }
  .chip.gen { color: var(--accent); background: var(--accent-soft); border-color: var(--accent); }

  /* Native <select> restyled as the accent team-switcher pill — the mockup used a bespoke menu; a
     real <select> is the lazy, accessible, keyboard-native equivalent (arrow supplied by the UA). */
  .teamsel {
    appearance: none;
    -webkit-appearance: none;
    padding-right: 26px;
  }
  .fielderr {
    margin-top: 6px;
    font-size: 0.75rem;
    color: var(--crit);
  }
  /* Task routing is the core of the page: section title carries page-level weight, rows read
     at body size instead of whispering. */
  /* the first section sits under the pagehead — its gap rules, no extra top margin */
  .secthead.core {
    margin: 0 0 var(--gap-section-body);
  }
  .secthead.core h2 {
    font-weight: calc(680 + (var(--ui-weight) - 400));
  }
  .secthead.core .hint {
    font-size: 0.8125rem;
  }
  :global(.tasktable .tasktype) {
    font-size: 0.8125rem;
    font-weight: calc(500 + (var(--ui-weight) - 400));
  }
  :global(.tasktable .taskdesc) {
    font-size: 0.75rem;
    max-width: 420px;
  }
  :global(.tasktable .routesel) {
    font-size: 0.8125rem;
    height: 32px;
  }
  :global(.tasktable td) {
    padding-top: calc(9px * var(--density));
    padding-bottom: calc(9px * var(--density));
  }
  .degradedcard .cb {
    padding: 12px 15px;
  }
  /* Task-type authoring aids in the Add/Edit modal — component-scoped (app.css is off-limits
     while the typography pass runs). */
  .descwarn {
    margin-top: 6px;
    font-size: 0.71875rem;
    color: var(--warn);
  }
  .descpair {
    display: flex;
    flex-direction: column;
    gap: 2px;
    margin-top: 7px;
    font-size: 0.71875rem;
  }
  .descpair .good {
    color: var(--good);
  }
  .descpair .bad {
    color: var(--text-3);
    text-decoration: line-through;
  }
  .exemplars {
    margin-top: 9px;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 7px 10px;
    background: var(--panel-2);
  }
  .exemplars summary {
    cursor: pointer;
    font-size: 0.71875rem;
    color: var(--text-2);
    user-select: none;
  }
  .exemplars summary:hover {
    color: var(--text);
  }
  .exemplars ul {
    margin: 7px 0 2px;
    padding-left: 16px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .exemplars li {
    font-size: 0.71875rem;
    color: var(--text-2);
    line-height: 1.45;
  }
  .exemplars li .n {
    color: var(--text);
  }

  /* Custom task-type row actions (Default column — a custom type has no default to show). */
  .ctacts {
    display: flex;
    gap: 6px;
  }
  /* Adopted-row remove: the disclose icon-button family, danger on hover. */
  .rmadopt:hover {
    color: var(--crit);
    background: var(--crit-soft);
  }
  .rmwarn {
    padding: 10px 12px;
    border: 1px solid var(--warn);
    background: var(--warn-soft);
    border-radius: 9px;
    font-size: 0.8125rem;
  }
  .rmwarn ul {
    margin: 6px 0 0;
    padding-left: 18px;
  }
  .rmwarn li {
    margin: 2px 0;
  }
  .rmnote {
    margin: 8px 0 0;
    font-size: 0.8125rem;
    color: var(--text-2);
  }
  .btn.danger {
    color: var(--crit);
  }
  .btn.danger.armed {
    border-color: var(--crit);
    background: var(--crit-soft);
  }
  /* operator org picker (tuning-page pattern) */
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
  .orgform input:focus {
    border-color: var(--accent-line);
    outline: none;
  }
  .fieldnote {
    margin-top: 6px;
    font-size: 0.6875rem;
    color: var(--text-3);
  }

  /* Model identity: real provider name is primary; the or-* routing alias is the demoted mono sub-label. */
  .mname {
    font-weight: calc(600 + (var(--ui-weight) - 400));
    color: var(--text);
    line-height: 1.2;
  }
  .malias {
    font-family: var(--mono);
    font-size: 0.6875rem;
    color: var(--text-3);
    margin-top: 1px;
    letter-spacing: -0.01em;
  }
  /* Provider badge — human provider name (Anthropic · OpenRouter …). Reuses the chip look, sans not mono. */
  .provbadge {
    display: inline-flex;
    align-items: center;
    font-size: 0.6875rem;
    font-weight: calc(550 + (var(--ui-weight) - 400));
    padding: 2px 8px;
    border-radius: 20px;
    border: 1px solid var(--line-2);
    color: var(--text-2);
    background: var(--panel-2);
    white-space: nowrap;
  }
  /* Price signal dot: cheap → premium, colored off the actual $/1k (see priceTier). */
  .pricedot {
    display: inline-block;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    margin-right: 6px;
    vertical-align: baseline;
    background: var(--text-3);
  }
  .pricedot[data-tier='cheap'] {
    background: var(--good);
  }
  .pricedot[data-tier='mid'] {
    background: var(--warn);
  }
  .pricedot[data-tier='premium'] {
    background: var(--accent);
  }
  /* Sortable price header */
  th.sortable {
    cursor: pointer;
    user-select: none;
  }
  th.sortable:hover {
    color: var(--text-2);
  }
  th.sortable.sorted {
    color: var(--accent);
  }
  .sortcaret {
    font-size: 0.625rem;
    opacity: 0.8;
  }
  /* Routing dropdowns now show pretty names — sans reads better than the mono default for prose names. */
  .routesel {
    font-family: var(--sans);
  }

  /* ============================================================
     Provider modules (Section B) + Fireworks sync (Section C).
     Each card sets --ph (the provider hue from benchmarks.js);
     light-dark() follows app.css color-scheme in BOTH themes.
     ============================================================ */
  .pmark {
    width: 26px;
    height: 26px;
    border-radius: 7px;
    flex: 0 0 auto;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-family: var(--mono);
    font-size: 0.6875rem;
    font-weight: calc(650 + (var(--ui-weight) - 400));
    color: light-dark(hsl(var(--ph) 58% 30%), hsl(var(--ph) 52% 74%));
    background: light-dark(hsl(var(--ph) 55% 52% / 0.13), hsl(var(--ph) 50% 62% / 0.16));
    border: 1px solid light-dark(hsl(var(--ph) 48% 44% / 0.4), hsl(var(--ph) 52% 66% / 0.36));
  }
  .provcard,
  .synccard {
    margin-bottom: 14px;
    border-left: 3px solid light-dark(hsl(var(--ph) 48% 44% / 0.55), hsl(var(--ph) 52% 66% / 0.5));
  }
  .provhead {
    background: linear-gradient(
      90deg,
      light-dark(hsl(var(--ph) 55% 52% / 0.07), hsl(var(--ph) 50% 62% / 0.09)),
      transparent 60%
    );
  }
  .pfacts {
    margin-left: auto;
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 8px;
    flex-wrap: wrap;
    font-size: 0.6875rem;
    color: var(--text-3);
  }
  .pfact {
    border: 1px solid var(--line-2);
    border-radius: 20px;
    padding: 1px 8px;
    color: var(--text-2);
    white-space: nowrap;
  }
  .pfact.n {
    font-size: 0.625rem;
  }
  .pfact.tuned,
  .ftbadge {
    color: light-dark(hsl(var(--ph) 58% 30%), hsl(var(--ph) 52% 74%));
    border: 1px solid light-dark(hsl(var(--ph) 48% 44% / 0.4), hsl(var(--ph) 52% 66% / 0.36));
    background: light-dark(hsl(var(--ph) 55% 52% / 0.1), hsl(var(--ph) 50% 62% / 0.12));
  }
  .ftbadge {
    display: inline-flex;
    margin-left: 8px;
    padding: 1px 8px;
    border-radius: 20px;
    font-size: 0.625rem;
    font-weight: calc(600 + (var(--ui-weight) - 400));
    vertical-align: 1px;
    white-space: nowrap;
  }
  .malias {
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
    margin-top: 3px;
  }
  .idchip {
    font-size: 0.625rem;
    color: var(--text-3);
    border: 1px solid var(--line-2);
    border-radius: 4px;
    padding: 0 5px;
  }
  .idchip.alias {
    color: var(--text-2);
    background: var(--panel-2);
  }
  th.dcol,
  td.dcol {
    width: 34px;
    text-align: center;
  }
  .disclose {
    all: unset;
    cursor: pointer;
    width: 24px;
    height: 24px;
    border-radius: 6px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    color: var(--text-3);
  }
  .disclose:hover {
    background: var(--panel-2);
    color: var(--text);
  }
  .disclose:focus-visible {
    outline: 1px solid var(--accent-line);
  }
  .disclose svg {
    width: 14px;
    height: 14px;
    stroke: currentColor;
    fill: none;
    stroke-width: 1.7;
    transition: transform 0.13s ease;
  }
  .disclose.open svg {
    transform: rotate(180deg);
  }
  .drow td {
    background: var(--panel-2);
  }
  .dgrid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 4px 26px;
    padding: 2px 1px;
  }
  .df {
    display: flex;
    align-items: baseline;
    gap: 10px;
    font-size: 0.6875rem;
    min-width: 0;
  }
  .df span {
    color: var(--text-3);
    flex: 0 0 84px;
  }
  .df b {
    color: var(--text-2);
    font-weight: calc(500 + (var(--ui-weight) - 400));
    font-size: 0.6875rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
  }
  .depchip {
    color: light-dark(hsl(var(--ph) 58% 30%), hsl(var(--ph) 52% 74%));
    border: 1px solid light-dark(hsl(var(--ph) 48% 44% / 0.4), hsl(var(--ph) 52% 66% / 0.36));
    border-radius: 4px;
    padding: 0 4px;
    font-size: 0.625rem;
  }

  /* ---- Fireworks sync panel ---- */
  .synchint {
    color: var(--text-3);
    font-size: 0.8125rem;
  }
  .synchint b {
    display: block;
    color: var(--text);
    font-size: 0.8125rem;
    font-weight: calc(600 + (var(--ui-weight) - 400));
    margin-bottom: 4px;
  }
  .synchint p {
    margin: 0;
    max-width: 560px;
    line-height: 1.55;
  }
  .syncsum {
    display: flex;
    align-items: baseline;
    gap: 14px;
    flex-wrap: wrap;
    margin-bottom: 10px;
  }
  .syncfacts {
    font-size: 0.6875rem;
    color: var(--text-3);
  }
  .syncempty {
    margin: 0;
    font-size: 0.75rem;
    color: var(--text-3);
  }
  .syncrows {
    display: flex;
    flex-direction: column;
  }
  .srow {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: calc(8px * var(--density)) 2px;
    border-bottom: 1px solid var(--line);
  }
  .srow:last-child {
    border-bottom: 0;
  }
  .srow .state {
    flex: 0 0 auto;
  }
  .srow .state.info .d {
    background: var(--text-3);
  }
  .stext {
    font-size: 0.75rem;
    color: var(--text-2);
    min-width: 0;
    flex: 1 1 auto;
  }
  .stext b {
    color: var(--text);
    font-weight: calc(600 + (var(--ui-weight) - 400));
  }
  .actbtn {
    flex: 0 0 auto;
  }
  .yamlblock {
    margin: 0;
    padding: 12px 14px;
    border-radius: 9px;
    background: var(--panel-2);
    border: 1px solid var(--line);
    font-size: 0.6875rem;
    line-height: 1.6;
    color: var(--text-2);
    white-space: pre;
    overflow-x: auto;
    user-select: all;
  }

  @media (max-width: 900px) {
    .dgrid {
      grid-template-columns: 1fr;
    }
  }
</style>
