<script>
  // Wave-2 Settings: Organization (real, owner-gated) + Appearance (real, theme.js) +
  // Identity/Danger zone (honest placeholders — thin-slice defers SSO/SCIM + destructive
  // org actions; no invented endpoints). Card/Chip/Modal/SegmentedControl + app.css classes
  // (.field/.notew/.btn/.chip) only — no new primitives.
  import Card from '$lib/components/Card.svelte';
  import Chip from '$lib/components/Chip.svelte';
  import Modal from '$lib/components/Modal.svelte';
  import SegmentedControl from '$lib/components/SegmentedControl.svelte';
  import SkeletonCard from '$lib/components/SkeletonCard.svelte';
  import Table from '$lib/components/Table.svelte';
  import Skeleton from '$lib/components/Skeleton.svelte';
  import { browser } from '$app/environment';
  // Inlined edition check (not $lib/edition.js) so the org-scoped card branches below fold
  // at build time and drop from the OSS bundle — see vite.config.js `define`.
  const OSS = typeof __EDITION__ !== 'undefined' && __EDITION__ === 'oss';
  import { query } from '$lib/api/resource.svelte.js';
  import {
    getOrg, renameOrg, setZeroRetention, listTokens, mintToken, revokeToken, rotateToken,
    getMemberships,
    getProviderKeys, putProviderKey, deleteProviderKey,
    getObservabilityKeys, putObservabilityKey, deleteObservabilityKey,
    getOrgSSO, putOrgSSO, generateScimToken, revokeScimToken,
    getOrgStorage, putOrgStorage, testOrgStorage,
    listServiceTokens, mintServiceToken, revokeServiceToken, revokeAllTokens, listOrgTokens,
    getTokenPolicy, setTokenPolicy,
  } from '$lib/api/admin.js';
  import { currentTheme, toggleTheme } from '$lib/theme.js';
  import { revealIn } from '$lib/motion.js';
  // Single-tenant connections (OSS): stored provider keys + local models. Both are
  // self-contained components; this page only mounts them.
  import ProviderConnections from '$lib/components/ProviderConnections.svelte';
  import AddLocalModel from '$lib/components/AddLocalModel.svelte';

  let lmOpen = $state(false);
  let lmAdded = $state(''); // id of the last catalog entry added, shown as confirmation

  // getOrg/renameOrg are both require_role("owner") server-side (toto_gateway/routes/
  // admin_tenancy.py) — a non-owner sees the query's own `forbidden` branch for the
  // whole card, not just the rename action.
  // The org card (getOrg/renameOrg/zero-retention) is enterprise-only — OSS skips the fetch and
  // hides the whole section, leaving a single-tenant page of Appearance + personal API keys.
  const org = query(() => getOrg(), { immediate: !OSS });

  let renameOpen = $state(false);
  let nameDraft = $state('');
  let saving = $state(false);
  let saveError = $state('');

  function openRename() {
    nameDraft = org.data?.name ?? '';
    saveError = '';
    renameOpen = true;
  }

  async function saveRename() {
    const next = nameDraft.trim();
    if (!next || next === org.data?.name) { renameOpen = false; return; }
    saving = true;
    saveError = '';
    try {
      await renameOrg(next);
      await org.reload();
      renameOpen = false;
    } catch (e) {
      saveError = e?.status === 403
        ? 'Only the org owner can rename the organization.'
        : (e?.message || 'Rename failed.');
    } finally {
      saving = false;
    }
  }

  // Zero-retention (W1-C4): org.data.zero_retention comes back on the org row; this only writes.
  let zrSaving = $state(false);
  let zrError = $state('');
  async function pickZeroRetention(next) {
    const on = next === 'on';
    if (on === !!org.data?.zero_retention) return;
    zrSaving = true;
    zrError = '';
    try {
      await setZeroRetention(on);
      await org.reload();
    } catch (e) {
      zrError = e?.status === 403
        ? 'Only an org owner or admin can change retention.'
        : (e?.message || 'Could not update retention.');
    } finally {
      zrSaving = false;
    }
  }

  let theme = $state(currentTheme());
  function pickTheme(next) {
    if (next !== theme) theme = toggleTheme();
  }

  // ---- API keys ----------------------------------------------------------------------------
  // Base URL a user pastes into the pi CLI / an OpenAI-compatible client. '' during prerender.
  const baseUrl = browser ? location.origin + '/v1' : '';

  const tokens = query(() => listTokens());
  // Multi-org (W2-C1): offer an org binding on mint when the user belongs to more than one org.
  const memberships = query(() => getMemberships());
  const orgs = $derived(memberships.data?.memberships ?? []);
  const multiOrg = $derived(orgs.length > 1);

  // Create-key modal: label -> mint -> show the secret ONCE (never re-shown after close).
  let createOpen = $state(false);
  let labelDraft = $state('');
  let orgDraft = $state(''); // '' = unbound (resolves to the default membership)
  let expiryDraft = $state(''); // '' = no expiry (subject to the org cap); else a day count
  let minting = $state(false);
  let mintError = $state('');
  let newSecret = $state(''); // the raw token, held only while the modal is open

  function openCreate() {
    labelDraft = '';
    orgDraft = '';
    expiryDraft = '';
    mintError = '';
    newSecret = '';
    createOpen = true;
  }

  async function doMint() {
    const label = labelDraft.trim();
    if (!label || minting || newSecret) return;
    minting = true;
    mintError = '';
    try {
      const res = await mintToken(label, orgDraft || undefined, Number(expiryDraft) || undefined);
      newSecret = res.token;
    } catch (e) {
      mintError = e?.status === 403
        ? 'Sign in with a user account to create keys — the operator credential cannot own API keys.'
        : (e?.message || 'Could not create key.');
    } finally {
      minting = false;
    }
  }

  // When the create modal closes AFTER a successful mint, wipe the secret and refresh the list.
  $effect(() => {
    if (!createOpen && newSecret) {
      newSecret = '';
      labelDraft = '';
      tokens.reload();
    }
  });

  // Revoke confirm.
  let revokeOpen = $state(false);
  let revokeTarget = $state(null); // the token row being revoked
  let revoking = $state(false);
  let revokeError = $state('');

  function askRevoke(t) {
    revokeTarget = t;
    revokeError = '';
    revokeOpen = true;
  }

  async function doRevoke() {
    if (!revokeTarget || revoking) return;
    revoking = true;
    revokeError = '';
    try {
      await revokeToken(revokeTarget.token_id);
      revokeOpen = false;
      await tokens.reload();
    } catch (e) {
      // 404 = already gone / not yours — treat as removed and refresh.
      if (e?.status === 404) { revokeOpen = false; await tokens.reload(); }
      else revokeError = e?.message || 'Could not revoke key.';
    } finally {
      revoking = false;
    }
  }

  // Rotate (W2-C3): mint a new secret for an existing key, shown ONCE. The old key keeps working
  // for the org's grace window, then dies — same shown-once modal shape as create.
  let rotateOpen = $state(false);
  let rotateTarget = $state(null);
  let rotating = $state(false);
  let rotateError = $state('');
  let rotatedSecret = $state('');

  function askRotate(t) {
    rotateTarget = t;
    rotateError = '';
    rotatedSecret = '';
    rotateOpen = true;
  }

  async function doRotate() {
    if (!rotateTarget || rotating || rotatedSecret) return;
    rotating = true;
    rotateError = '';
    try {
      rotatedSecret = (await rotateToken(rotateTarget.token_id)).token;
    } catch (e) {
      rotateError = e?.status === 404 ? 'That key is already gone.' : (e?.message || 'Could not rotate key.');
    } finally {
      rotating = false;
    }
  }

  $effect(() => {
    if (!rotateOpen && rotatedSecret) { rotatedSecret = ''; rotateTarget = null; tokens.reload(); }
  });

  // ago() reads created/last_used; expiry needs a forward-looking "in 12d" / "expired". A far-future
  // stamp (decades out) reads as "never".
  function expiryLabel(secs) {
    if (secs == null) return '—';
    const d = secs - Date.now() / 1000;
    if (d > 40 * 365 * 86400) return 'never';
    if (d <= 0) return 'expired';
    if (d < 86400) return `in ${Math.floor(d / 3600)}h`;
    if (d < 86400 * 60) return `in ${Math.floor(d / 86400)}d`;
    return new Date(secs * 1000).toLocaleDateString();
  }

  // ---- Organization tokens (W2-C3 admin): service tokens + compliance list + lifetime/grace ----
  // Every query drives its own forbidden/empty branch, so a non-admin caller simply sees the
  // forbidden note rather than a broken card (no client-side role probe needed).
  // Org-token admin (service tokens, compliance list, policy) is enterprise-only; the render is
  // already !OSS-gated below, and immediate:!OSS keeps the eager fetch from 404ing in OSS.
  const svcTokens = query(() => listServiceTokens(), { immediate: !OSS });
  const orgCreds = query(() => listOrgTokens(), { immediate: !OSS });
  const tokenPolicy = query(() => getTokenPolicy(), { immediate: !OSS });

  // Service-token mint (shown once, admin-gated server-side).
  let svcOpen = $state(false);
  let svcLabel = $state('');
  let svcMinting = $state(false);
  let svcError = $state('');
  let svcSecret = $state('');
  function openSvc() { svcLabel = ''; svcError = ''; svcSecret = ''; svcOpen = true; }
  async function doSvcMint() {
    const label = svcLabel.trim();
    if (!label || svcMinting || svcSecret) return;
    svcMinting = true; svcError = '';
    try {
      svcSecret = (await mintServiceToken(label)).token;
    } catch (e) {
      svcError = e?.status === 403 ? 'Only an org owner or admin can create service tokens.'
        : (e?.message || 'Could not create service token.');
    } finally { svcMinting = false; }
  }
  $effect(() => {
    if (!svcOpen && svcSecret) { svcSecret = ''; svcLabel = ''; svcTokens.reload(); orgCreds.reload(); }
  });
  async function doSvcRevoke(t) {
    try { await revokeServiceToken(t.token_id); } catch { /* 404 = already gone */ }
    await svcTokens.reload(); await orgCreds.reload();
  }

  // Bulk revoke (owner, org-wide) — the incident-response kill for every token in the org. Sessions
  // are included by default (that's what makes "everyone gets signed out" true); service tokens are
  // spared unless explicitly checked (CI keeps running). Backend is owner-gated (403 for an admin).
  let bulkOpen = $state(false);
  let bulkOrgWide = $state(true);   // this screen only has the org-wide mode (no per-user picker here)
  let bulkSessions = $state(true);
  let bulkService = $state(false);
  let bulkRunning = $state(false);
  let bulkErr = $state('');
  let bulkResult = $state(null);    // {counts:{api, session?, service?, total}}
  function openBulk() {
    bulkOrgWide = true; bulkSessions = true; bulkService = false;
    bulkErr = ''; bulkResult = null; bulkOpen = true;
  }
  async function doBulkRevoke() {
    if (bulkRunning || bulkResult) return;
    bulkRunning = true; bulkErr = '';
    try {
      const r = await revokeAllTokens(
        { org_wide: true, include_sessions: bulkSessions, include_service: bulkService });
      bulkResult = r;
      await Promise.all([svcTokens.reload(), orgCreds.reload()]);
    } catch (e) {
      bulkErr = e?.status === 403 ? 'Only the org owner can revoke every token in the organization.'
        : (e?.message || 'Could not revoke tokens.');
    } finally { bulkRunning = false; }
  }

  // Token policy (lifetime cap + rotation grace). Draft mirrors the loaded values.
  let policyMaxDays = $state('');
  let policyGrace = $state('');
  let policySaving = $state(false);
  let policyError = $state('');
  let policySaved = $state(false);
  $effect(() => {
    if (tokenPolicy.data) {
      policyMaxDays = String(tokenPolicy.data.max_token_lifetime_days ?? 0);
      policyGrace = String(tokenPolicy.data.token_rotation_grace_minutes ?? 60);
    }
  });
  async function savePolicy() {
    policySaving = true; policyError = ''; policySaved = false;
    try {
      await setTokenPolicy(Number(policyMaxDays) || 0, Number(policyGrace) || 0);
      policySaved = true;
      await tokenPolicy.reload();
    } catch (e) {
      policyError = e?.status === 403 ? 'Only an org owner or admin can change token policy.'
        : (e?.message || 'Could not save policy.');
    } finally { policySaving = false; }
  }

  // ---- Provider keys -----------------------------------------------------------------------
  // Two groups, one card: inference keys (org-wide BYOK — routes/org_credentials.py) and
  // governance keys (provider org-ADMIN keys for observability — routes/admin_observability.py).
  // Raw keys go up once over the session cookie and are never returned; the UI only ever holds
  // configured + last4.
  const provKeys = query(() => getProviderKeys(), { immediate: !OSS });
  const govKeys = query(() => getObservabilityKeys(), { immediate: !OSS });

  const GOV_PROVIDERS = [
    { provider: 'anthropic', label: 'Anthropic', hint: 'Organization admin key — spend, usage, and member activity as Anthropic reports it.' },
    { provider: 'openai', label: 'OpenAI', hint: 'Organization admin key — spend, usage, and member activity as OpenAI reports it.' },
  ];

  // Set/replace modal. `keyTarget.kind` picks the API family; the input is type=password so the
  // key never lands in autofill/screen-share plaintext.
  let keyOpen = $state(false);
  let keyTarget = $state(null); // { kind: 'inference'|'governance', provider, label, replacing }
  let keyDraft = $state('');
  let keySaving = $state(false);
  let keyError = $state('');

  function openKeyModal(kind, provider, label, replacing) {
    keyTarget = { kind, provider, label, replacing };
    keyDraft = '';
    keyError = '';
    keyOpen = true;
  }

  async function saveKey() {
    const v = keyDraft.trim();
    if (!v || keySaving || !keyTarget) return;
    keySaving = true;
    keyError = '';
    try {
      if (keyTarget.kind === 'inference') await putProviderKey(keyTarget.provider, v);
      else await putObservabilityKey(keyTarget.provider, v);
      keyOpen = false;
      await (keyTarget.kind === 'inference' ? provKeys.reload() : govKeys.reload());
    } catch (e) {
      keyError = e?.status === 403
        ? 'Only the org owner can manage provider keys.'
        : e?.status === 503
          ? 'This deploy has no key-encryption secret configured — keys can’t be stored yet.'
          : (e?.message || 'Could not save the key.');
    } finally {
      keySaving = false;
    }
  }

  // Wipe the draft the moment the modal closes — the raw key never outlives the dialog.
  $effect(() => {
    if (!keyOpen) { keyDraft = ''; keyError = ''; }
  });

  // Remove confirm.
  let pkRemoveOpen = $state(false);
  let pkRemoveTarget = $state(null); // { kind, provider, label }
  let pkRemoving = $state(false);
  let pkRemoveError = $state('');

  function askRemoveKey(kind, provider, label) {
    pkRemoveTarget = { kind, provider, label };
    pkRemoveError = '';
    pkRemoveOpen = true;
  }

  async function doRemoveKey() {
    if (!pkRemoveTarget || pkRemoving) return;
    pkRemoving = true;
    pkRemoveError = '';
    try {
      if (pkRemoveTarget.kind === 'inference') await deleteProviderKey(pkRemoveTarget.provider);
      else await deleteObservabilityKey(pkRemoveTarget.provider);
      pkRemoveOpen = false;
      await (pkRemoveTarget.kind === 'inference' ? provKeys.reload() : govKeys.reload());
    } catch (e) {
      pkRemoveError = e?.status === 403
        ? 'Only the org owner can remove provider keys.'
        : (e?.message || 'Could not remove the key.');
    } finally {
      pkRemoving = false;
    }
  }

  // ---- SSO (OIDC, owner-gated) -------------------------------------------------------------
  // toto_gateway/routes/admin_sso.py. Client secret is write-only — never prefilled, never shown.
  const sso = query(() => getOrgSSO(), { immediate: !OSS });
  const ssoRedirectUri = browser ? location.origin + '/v1/auth/sso/callback' : '';

  let ssoOpen = $state(false);
  // scimRows: [{group, role}] editor over the scim_group_role_map dict (W2-C2). Owner is not a
  // choice — SCIM can never grant ownership.
  let ssoDraft = $state({ issuer: '', client_id: '', client_secret: '', domains: '',
                          sso_required: false, scim_enabled: false, scimRows: [] });
  let ssoSaving = $state(false);
  let ssoError = $state('');
  const SCIM_ROLES = ['member', 'admin', 'auditor'];

  function openSSO() {
    const d = sso.data;
    ssoDraft = {
      issuer: d?.issuer ?? '',
      client_id: d?.client_id ?? '',
      client_secret: '', // write-only — never prefilled
      domains: (d?.domains ?? []).join(', '),
      sso_required: d?.sso_required ?? false,
      scim_enabled: d?.scim_enabled ?? false,
      scimRows: Object.entries(d?.scim_group_role_map ?? {}).map(([group, role]) => ({ group, role })),
    };
    ssoError = '';
    ssoOpen = true;
  }

  async function saveSSO() {
    if (ssoSaving) return;
    const domains = ssoDraft.domains.split(',').map((s) => s.trim()).filter(Boolean);
    const scim_group_role_map = {};
    for (const { group, role } of ssoDraft.scimRows) {
      const g = group.trim();
      if (g) scim_group_role_map[g] = role;
    }
    ssoSaving = true;
    ssoError = '';
    try {
      await putOrgSSO({
        issuer: ssoDraft.issuer.trim(),
        client_id: ssoDraft.client_id.trim(),
        client_secret: ssoDraft.client_secret.trim(),
        domains,
        sso_required: ssoDraft.sso_required,
        scim_enabled: ssoDraft.scim_enabled,
        scim_group_role_map,
      });
      await sso.reload();
      ssoOpen = false;
    } catch (e) {
      ssoError = e?.status === 403
        ? 'Only the org owner can configure SSO.'
        : e?.status === 409
          ? 'One of those domains is already used by another organization.'
          : e?.status === 503
            ? 'This deploy has no key-encryption secret configured — SSO can’t be stored yet.'
            : e?.code === 'bad_issuer'
              ? 'Issuer must be an https:// URL.'
              : (e?.message || 'Could not save SSO settings.');
    } finally {
      ssoSaving = false;
    }
  }

  // ---- Storage connector (BYOS, admin-gated) -------------------------------------------------
  // toto_gateway/routes/admin_storage.py. The org's private S3-compatible bucket for object
  // writes (documents, artifacts). Bucket secret is write-only — never prefilled, never shown.
  // Save is gated on a passing connection test of EXACTLY the fields being saved: `stTestedFp`
  // fingerprints the connector at test time, and any edit invalidates it.
  const orgStorage = query(() => getOrgStorage(), { immediate: !OSS });

  let stOpen = $state(false);
  let stDraft = $state({ enabled: false, s3_endpoint: '', s3_bucket: '', s3_region: 'us-east-1',
                         s3_access_key: '', s3_secret: '', s3_force_path_style: true });
  let stSaving = $state(false);
  let stError = $state('');
  let stTesting = $state(false);
  let stTested = $state(null);   // null | {ok, error} — the last test result for stTestedFp
  let stTestedFp = $state('');

  const stFp = (d) => JSON.stringify([d.s3_endpoint.trim(), d.s3_bucket.trim(),
    d.s3_region.trim(), d.s3_access_key.trim(), d.s3_secret, d.s3_force_path_style]);
  const stComplete = $derived(!!(stDraft.s3_endpoint.trim() && stDraft.s3_bucket.trim()
    && (stDraft.s3_secret.trim() || orgStorage.data?.has_s3_secret)));
  const stVerified = $derived(stTested?.ok && stTestedFp === stFp(stDraft));
  // The modal's Save always ENABLES the connector (picking "Your own bucket" is the enable),
  // so it always requires a green test of these exact fields.
  const stCanSave = $derived(!stSaving && stComplete && stVerified);

  // Mode picker on the card: 'toto' (default — the gateway's managed storage) vs 'byos'.
  // Picking 'byos' opens the modal (nothing saves until a tested Save); picking 'toto'
  // disables the connector in place, keeping the config (and stored secret) as a draft.
  const stMode = $derived(orgStorage.data?.enabled ? 'byos' : 'toto');
  let stModeSaving = $state(false);
  let stModeError = $state('');
  async function pickStorageMode(next) {
    if (next === stMode || stModeSaving) return;
    if (next === 'byos') { openStorage(); return; }
    stModeSaving = true;
    stModeError = '';
    try {
      const d = orgStorage.data;
      await putOrgStorage({
        enabled: false, s3_endpoint: d?.s3_endpoint ?? '', s3_bucket: d?.s3_bucket ?? '',
        s3_region: d?.s3_region || 'us-east-1', s3_access_key: d?.s3_access_key ?? '',
        s3_secret: '', s3_force_path_style: d?.s3_force_path_style ?? true,
      });
      await orgStorage.reload();
    } catch (e) {
      stModeError = e?.status === 403 ? 'Only an org owner or admin can change storage.'
        : (e?.message || 'Could not switch to Toto storage.');
    } finally {
      stModeSaving = false;
    }
  }

  function openStorage() {
    const d = orgStorage.data;
    stDraft = {
      enabled: d?.enabled ?? false,
      s3_endpoint: d?.s3_endpoint ?? '',
      s3_bucket: d?.s3_bucket ?? '',
      s3_region: d?.s3_region || 'us-east-1',
      s3_access_key: d?.s3_access_key ?? '',
      s3_secret: '', // write-only — never prefilled
      s3_force_path_style: d?.s3_force_path_style ?? true,
    };
    stError = '';
    stTested = null;
    stTestedFp = '';
    stOpen = true;
  }

  async function testStorage() {
    if (stTesting || !stComplete) return;
    stTesting = true;
    stError = '';
    const fp = stFp(stDraft);
    try {
      stTested = await testOrgStorage({
        s3_endpoint: stDraft.s3_endpoint.trim(), s3_bucket: stDraft.s3_bucket.trim(),
        s3_region: stDraft.s3_region.trim() || 'us-east-1',
        s3_access_key: stDraft.s3_access_key.trim(), s3_secret: stDraft.s3_secret.trim(),
        s3_force_path_style: stDraft.s3_force_path_style,
      });
      stTestedFp = fp;
    } catch (e) {
      stTested = null;
      stError = e?.status === 403 ? 'Only an org owner or admin can manage storage.'
        : e?.status === 503 ? 'This deploy has no key-encryption secret configured — bucket secrets can’t be stored yet.'
          : (e?.message || 'Connection test failed to run.');
    } finally {
      stTesting = false;
    }
  }

  async function saveStorage() {
    if (!stCanSave) return;
    stSaving = true;
    stError = '';
    try {
      await putOrgStorage({
        enabled: true, s3_endpoint: stDraft.s3_endpoint.trim(),
        s3_bucket: stDraft.s3_bucket.trim(), s3_region: stDraft.s3_region.trim() || 'us-east-1',
        s3_access_key: stDraft.s3_access_key.trim(), s3_secret: stDraft.s3_secret.trim(),
        s3_force_path_style: stDraft.s3_force_path_style,
      });
      await orgStorage.reload();
      stOpen = false;
    } catch (e) {
      stError = e?.status === 403 ? 'Only an org owner or admin can manage storage.'
        : e?.status === 503 ? 'This deploy has no key-encryption secret configured — bucket secrets can’t be stored yet.'
          : (e?.message || 'Could not save the storage connector.');
    } finally {
      stSaving = false;
    }
  }

  // Wipe the secret draft the moment the modal closes — it never outlives the dialog.
  $effect(() => {
    if (!stOpen) { stDraft.s3_secret = ''; stError = ''; stTested = null; stTestedFp = ''; }
  });

  // ---- SCIM token (owner-gated, shown-once) ------------------------------------------------
  let scimToken = $state('');       // the raw token, shown ONCE right after generate/rotate
  let scimBusy = $state(false);
  let scimError = $state('');

  async function genScimToken() {
    if (scimBusy) return;
    scimBusy = true; scimError = ''; scimToken = '';
    try {
      scimToken = (await generateScimToken()).token;
      await sso.reload();
    } catch (e) {
      scimError = e?.status === 403 ? 'Only the org owner can manage the SCIM token.'
        : (e?.message || 'Could not generate the SCIM token.');
    } finally { scimBusy = false; }
  }

  async function revokeScim() {
    if (scimBusy) return;
    scimBusy = true; scimError = ''; scimToken = '';
    try {
      await revokeScimToken();
      await sso.reload();
    } catch (e) {
      scimError = e?.status === 403 ? 'Only the org owner can manage the SCIM token.'
        : (e?.message || 'Could not revoke the SCIM token.');
    } finally { scimBusy = false; }
  }

  // Copy-to-clipboard with a brief "Copied" flash keyed by field name.
  let copied = $state('');
  async function copy(text, key) {
    try {
      await navigator.clipboard.writeText(text);
      copied = key;
      setTimeout(() => { if (copied === key) copied = ''; }, 1600);
    } catch { /* clipboard blocked — the value is selectable in the field regardless */ }
  }

  // Unix-seconds -> compact "3d ago" / date. Tokens store created_at/last_used as REAL epoch secs.
  function ago(secs) {
    if (secs == null) return null;
    const d = Math.max(0, Date.now() / 1000 - secs);
    if (d < 60) return 'just now';
    if (d < 3600) return `${Math.floor(d / 60)}m ago`;
    if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
    if (d < 86400 * 30) return `${Math.floor(d / 86400)}d ago`;
    return new Date(secs * 1000).toLocaleDateString();
  }
  const createdOf = (t) => t.created ?? t.created_at; // route says `created`; store column is created_at
</script>

<svelte:head><title>Settings · Toto Control</title></svelte:head>

<div class="pagehead">
  <div>
    <h1>Settings</h1>
    <div class="sub">{OSS ? 'Appearance, API keys, and provider connections.' : 'Organization profile, appearance, identity, and danger zone.'}</div>
  </div>
</div>

<!-- ========================= ORGANIZATION ========================= -->
{#if !OSS}
{#if org.status === 'loading'}
  <SkeletonCard lines={2} />
{:else if org.status === 'unauthed'}
  <Card title="Organization">
    <div class="notew"><span>You're signed out — sign in to view organization settings.</span></div>
  </Card>
{:else if org.status === 'forbidden'}
  <Card title="Organization">
    <div class="notew"><span>Organization settings are visible to the org owner only.</span></div>
  </Card>
{:else if org.status === 'error'}
  <Card title="Organization">
    <div class="notew"><span>Couldn't load organization: {org.error?.message}</span></div>
  </Card>
{:else}
  <div in:revealIn>
    <Card title="Organization">
      <div class="settingsrow">
        <div>
          <div class="orgname">{org.data.name}</div>
          <div class="muted mono">{org.data.org_id}</div>
        </div>
        <button class="btn" onclick={openRename}>Rename</button>
      </div>
      <div class="settingsrow" style="border-top:1px solid var(--line);padding-top:14px;margin-top:14px;">
        <div>
          <b>Zero retention</b>
          <p class="hint">Never store prompt or response text on the gateway. Metadata (tokens, cost,
            latency) is always kept, so routing analytics and billing keep working.</p>
          {#if zrError}<p class="hint" style="color:var(--warn)">{zrError}</p>{/if}
        </div>
        <SegmentedControl
          options={[{ value: 'off', label: 'Off' }, { value: 'on', label: 'On' }]}
          value={org.data.zero_retention ? 'on' : 'off'}
          onchange={pickZeroRetention}
          disabled={zrSaving}
        />
      </div>
    </Card>
  </div>
{/if}
{/if}

<!-- ========================= APPEARANCE ========================= -->
<Card title="Appearance">
  <div class="settingsrow">
    <div>
      <b>Theme</b>
      <p class="hint">Light is the default; forest-dark for low-light work. Persists to this browser.</p>
    </div>
    <SegmentedControl
      options={[{ value: 'light', label: 'Light' }, { value: 'dark', label: 'Forest dark' }]}
      value={theme}
      onchange={pickTheme}
    />
  </div>
  <div class="notew">
    <svg viewBox="0 0 24 24"><path d="M12 9v4M12 17h.01" /><path d="M10.3 3.9 2 18a2 2 0 0 0 1.7 3h16.6A2 2 0 0 0 22 18L13.7 3.9a2 2 0 0 0-3.4 0z" /></svg>
    <span>Font, weight, line height, and density live under the <b>⚙</b> tuning menu (bottom-right of every page) — this control only sets light / dark.</span>
  </div>
</Card>

<!-- ========================= API KEYS ========================= -->
<Card>
  {#snippet head()}
    <h3>API keys</h3>
    <button
      class="btn primary headbtn"
      onclick={openCreate}
      disabled={tokens.status === 'unauthed' || tokens.status === 'forbidden'}
    >Create API key</button>
  {/snippet}

  <p class="hint">Bearer tokens for the API — e.g. pointing the pi CLI or any OpenAI-compatible client at this gateway.</p>

  {#if baseUrl}
    <div class="baseurl">
      <span class="bl">Base URL</span>
      <code class="mono">{baseUrl}</code>
      <button class="btn ghost small" onclick={() => copy(baseUrl, 'url')}>{copied === 'url' ? 'Copied' : 'Copy'}</button>
    </div>
  {/if}

  {#if tokens.status === 'loading'}
    <div class="skrows">
      <Skeleton height="16px" />
      <Skeleton height="16px" width="70%" />
    </div>
  {:else if tokens.status === 'unauthed'}
    <div class="notew"><span>You're signed out — sign in to manage API keys.</span></div>
  {:else if tokens.status === 'forbidden'}
    <div class="notew"><span>Sign in with a user account to manage keys — the operator credential cannot own API keys.</span></div>
  {:else if tokens.status === 'error'}
    <div class="notew"><span>Couldn't load API keys: {tokens.error?.message}</span></div>
  {:else if tokens.status === 'empty'}
    <div class="emptyk">No API keys yet — create one to use the API.</div>
  {:else}
    <div in:revealIn>
      <Table>
        {#snippet head()}
          <tr><th>Label</th>{#if multiOrg}<th>Organization</th>{/if}<th>Created</th><th>Last used</th><th>Expires</th><th></th></tr>
        {/snippet}
        {#each tokens.data.tokens as t (t.token_id)}
          <tr>
            <td>{t.label}</td>
            {#if multiOrg}<td class="muted">{t.org_name ?? 'Default'}</td>{/if}
            <td class="muted">{ago(createdOf(t)) ?? '—'}</td>
            <td class="muted">{t.last_used == null ? 'never' : ago(t.last_used)}</td>
            <td class="muted">{expiryLabel(t.expires_at)}</td>
            <td class="ta-r">
              <button class="btn small" onclick={() => askRotate(t)}>Rotate</button>
              <button class="btn small" onclick={() => askRevoke(t)}>Revoke</button>
            </td>
          </tr>
        {/each}
      </Table>
    </div>
  {/if}
</Card>

<!-- ========================= CONNECTIONS (single-tenant OSS) ========================= -->
{#if OSS}
  <ProviderConnections />
  <Card title="Local models">
    <div class="settingsrow">
      <div>
        <b>Your own hardware</b>
        <p class="hint">Point the router at an OpenAI-compatible server on your machine or
          network — Ollama, LM Studio, vLLM. No API key needed.
          {#if lmAdded}<b>Added {lmAdded} to the catalog.</b>{/if}</p>
      </div>
      <button class="btn primary" onclick={() => (lmOpen = true)}>Add local model</button>
    </div>
  </Card>
  <AddLocalModel bind:open={lmOpen} onadded={(entry) => (lmAdded = entry?.id ?? 'model')} />
{/if}

<!-- ========================= ORGANIZATION TOKENS (admin) ========================= -->
<!-- Org-scoped cards below (service tokens, org provider keys, storage connector, SSO,
     delete-org) are enterprise-only; user-level API keys above stay in OSS. Their modals
     further down stay compiled but are unreachable without these cards. -->
{#if !OSS && svcTokens.status !== 'unauthed' && svcTokens.status !== 'forbidden'}
  <div in:revealIn>
    <Card>
      {#snippet head()}
        <h3>Organization tokens</h3>
        <button class="btn primary headbtn" onclick={openSvc}>Create service token</button>
      {/snippet}
      <p class="hint">Service tokens are owned by the organization, not a person — use them for CI and
        automation. They survive when a member leaves (unlike personal keys).</p>

      {#if svcTokens.status === 'empty'}
        <div class="emptyk">No service tokens yet.</div>
      {:else if svcTokens.status === 'ok'}
        <Table>
          {#snippet head()}
            <tr><th>Label</th><th>Created</th><th>Last used</th><th>Expires</th><th></th></tr>
          {/snippet}
          {#each svcTokens.data.tokens as t (t.token_id)}
            <tr>
              <td>{t.label}</td>
              <td class="muted">{ago(t.created_at) ?? '—'}</td>
              <td class="muted">{t.last_used == null ? 'never' : ago(t.last_used)}</td>
              <td class="muted">{expiryLabel(t.expires_at)}</td>
              <td class="ta-r"><button class="btn small" onclick={() => doSvcRevoke(t)}>Revoke</button></td>
            </tr>
          {/each}
        </Table>
      {/if}

      <!-- Token policy: mint lifetime cap + rotation grace -->
      <div class="settingsrow" style="border-top:1px solid var(--line);padding-top:14px;margin-top:16px;">
        <div>
          <b>Token policy</b>
          <p class="hint">The maximum lifetime a new key may request (0 = no cap; existing keys are
            not changed), and how long a rotated-out secret keeps working.</p>
        </div>
      </div>
      <div class="policyrow">
        <div class="field">
          <label for="pol-days">Max lifetime (days, 0 = no cap)</label>
          <input id="pol-days" type="number" min="0" bind:value={policyMaxDays} />
        </div>
        <div class="field">
          <label for="pol-grace">Rotation grace (minutes)</label>
          <input id="pol-grace" type="number" min="0" bind:value={policyGrace} />
        </div>
        <button class="btn primary" onclick={savePolicy} disabled={policySaving}>
          {policySaving ? 'Saving…' : policySaved ? 'Saved' : 'Save policy'}
        </button>
      </div>
      {#if policyError}<div class="notew"><span>{policyError}</span></div>{/if}

      <!-- Compliance list: every live credential in the org -->
      <div class="settingsrow" style="border-top:1px solid var(--line);padding-top:14px;margin-top:16px;">
        <div>
          <b>All credentials</b>
          <p class="hint">Every live key in the organization — owner, purpose, age, and last use.</p>
        </div>
      </div>
      {#if orgCreds.status === 'empty'}
        <div class="emptyk">No credentials.</div>
      {:else if orgCreds.status === 'ok'}
        <Table>
          {#snippet head()}
            <tr><th>Owner / label</th><th>Type</th><th>Created</th><th>Last used</th><th>Expires</th></tr>
          {/snippet}
          {#each orgCreds.data.credentials as c (c.token_id)}
            <tr>
              <td>{c.owner_email ?? c.label ?? c.token_id}</td>
              <td><Chip>{c.purpose === 'service' ? 'service' : 'personal'}</Chip></td>
              <td class="muted">{ago(c.created_at) ?? '—'}</td>
              <td class="muted">{c.last_used == null ? 'never' : ago(c.last_used)}</td>
              <td class="muted">{expiryLabel(c.expires_at)}</td>
            </tr>
          {/each}
        </Table>
      {/if}

      <!-- Bulk revoke: the org-wide incident-response kill (owner-gated server-side). -->
      <div class="settingsrow bulkzone" style="border-top:1px solid var(--line);padding-top:14px;margin-top:16px;">
        <div>
          <b>Revoke all tokens</b>
          <p class="hint">Sign everyone out and invalidate every personal API key in the organization at
            once — use this if a credential may be compromised.</p>
        </div>
        <button class="btn danger" onclick={openBulk}>Revoke all…</button>
      </div>
    </Card>
  </div>
{/if}

<!-- ========================= PROVIDER KEYS ========================= -->
{#if !OSS}
<Card title="Provider keys">
  <p class="hint">Connect your organization's own provider accounts. Keys are encrypted before
    they're stored and never shown again — only the last 4 characters are kept so you can
    recognize them. Remove a key at any time.</p>

  <div class="pkgroup">
    <div class="pkghead">Inference</div>
    <p class="hint">Org-wide keys your traffic runs on. A member's personal key (set in the app)
      takes precedence; without either, requests use the platform's shared credentials.</p>
    {#if provKeys.status === 'loading'}
      <div class="skrows"><Skeleton height="16px" /><Skeleton height="16px" width="70%" /></div>
    {:else if provKeys.status === 'unauthed'}
      <div class="notew"><span>You're signed out — sign in to manage provider keys.</span></div>
    {:else if provKeys.status === 'forbidden'}
      <div class="notew"><span>Provider keys are visible to org admins only.</span></div>
    {:else if provKeys.status === 'error'}
      <div class="notew"><span>Couldn't load provider keys: {provKeys.error?.message}</span></div>
    {:else}
      {#each provKeys.data.keys as k (k.provider)}
        <div class="pkrow">
          <div>
            <b>{k.label}</b>
            <p class="hint">{k.powers}</p>
          </div>
          <div class="pkactions">
            {#if k.configured}
              <code class="mono pkhint">•••• {k.last4 || '????'}</code>
              <button class="btn small" onclick={() => openKeyModal('inference', k.provider, k.label, true)}>Replace</button>
              <button class="btn small" onclick={() => askRemoveKey('inference', k.provider, k.label)}>Remove</button>
            {:else}
              <button class="btn small primary" onclick={() => openKeyModal('inference', k.provider, k.label, false)}>Add key</button>
            {/if}
          </div>
        </div>
      {/each}
    {/if}
  </div>

  <div class="pkgroup">
    <div class="pkghead">Governance &amp; monitoring</div>
    <p class="hint">Read-only organization admin keys. Toto uses them to pull the provider's own
      view of spend, usage, and member activity for governance dashboards — they are verified
      with the provider before being saved and are never used to run traffic.</p>
    {#if govKeys.status === 'loading'}
      <div class="skrows"><Skeleton height="16px" /><Skeleton height="16px" width="70%" /></div>
    {:else if govKeys.status === 'unauthed'}
      <div class="notew"><span>You're signed out — sign in to manage governance keys.</span></div>
    {:else if govKeys.status === 'forbidden'}
      <div class="notew"><span>Governance keys are visible to org admins only.</span></div>
    {:else if govKeys.status === 'error'}
      <div class="notew"><span>Couldn't load governance keys: {govKeys.error?.message}</span></div>
    {:else}
      {#each GOV_PROVIDERS as g (g.provider)}
        {@const st = govKeys.data.keys?.[g.provider] ?? {}}
        <div class="pkrow">
          <div>
            <b>{g.label}</b>
            <p class="hint">{g.hint}</p>
          </div>
          <div class="pkactions">
            {#if st.configured}
              {#if st.org_name}<Chip>{st.org_name}</Chip>{/if}
              <code class="mono pkhint">•••• {st.last4 || '????'}</code>
              <button class="btn small" onclick={() => openKeyModal('governance', g.provider, g.label, true)}>Replace</button>
              <button class="btn small" onclick={() => askRemoveKey('governance', g.provider, g.label)}>Remove</button>
            {:else}
              <button class="btn small primary" onclick={() => openKeyModal('governance', g.provider, g.label, false)}>Add key</button>
            {/if}
          </div>
        </div>
      {/each}
    {/if}
  </div>
</Card>

<!-- ========================= IDENTITY / SSO ========================= -->
<Card title="Storage">
  {#if orgStorage.status === 'loading'}
    <div class="skrows"><Skeleton height="16px" /><Skeleton height="16px" width="60%" /></div>
  {:else if orgStorage.status === 'unauthed'}
    <div class="notew"><span>You're signed out — sign in to manage storage.</span></div>
  {:else if orgStorage.status === 'forbidden'}
    <div class="comingsoon">
      <div class="chead"><b>Where your files live</b></div>
      <p class="hint">Toto stores your team's files by default. An organization owner or admin
        can switch to your own bucket here.</p>
    </div>
  {:else if orgStorage.status === 'error'}
    <div class="notew"><span>Couldn't load storage settings: {orgStorage.error?.message}</span></div>
  {:else}
    <div class="settingsrow">
      <div>
        <b>Where your files live</b>
        {#if orgStorage.data.enabled}
          <p class="hint">Documents and files Toto saves for your team are written to your own
            bucket. Switch back to Toto storage any time — your connector stays saved.</p>
        {:else}
          <p class="hint">Toto stores your team's documents and files for you — nothing to set
            up. Prefer your own S3-compatible bucket (AWS, MinIO, Cloudflare R2…)? Switch over
            and connect it.</p>
        {/if}
        {#if stModeError}<p class="hint" style="color:var(--warn)">{stModeError}</p>{/if}
      </div>
      <SegmentedControl
        options={[{ value: 'toto', label: 'Toto storage' }, { value: 'byos', label: 'Your own bucket' }]}
        value={stMode}
        onchange={pickStorageMode}
        disabled={stModeSaving}
      />
    </div>
    {#if orgStorage.data.enabled}
      <div class="ssometa">
        <div class="ssorow"><span class="k">Endpoint</span><code class="mono">{orgStorage.data.s3_endpoint}</code></div>
        <div class="ssorow"><span class="k">Bucket</span><code class="mono">{orgStorage.data.s3_bucket}</code></div>
        <div class="ssorow"><span class="k">Region</span><code class="mono">{orgStorage.data.s3_region}</code></div>
        <div class="ssorow"><span class="k">Bucket secret</span>
          <span>{orgStorage.data.has_s3_secret ? 'Saved — stored encrypted, never shown' : 'Not set'}</span></div>
        {#if orgStorage.data.last_test}
          <div class="ssorow"><span class="k">Last test</span>
            <span>{orgStorage.data.last_error ? `Failed — ${orgStorage.data.last_error}` : 'Connection verified'}</span></div>
        {/if}
        <div class="settingsrow" style="margin-top:6px;">
          <span></span>
          <button class="btn small" onclick={openStorage}>Edit connector</button>
        </div>
      </div>
    {:else if orgStorage.data.configured}
      <p class="hint">A bucket connector is saved but not in use — pick “Your own bucket” to
        re-verify and switch back to it.</p>
    {/if}
  {/if}
</Card>

<Card title="Identity">
  {#if sso.status === 'loading'}
    <div class="skrows"><Skeleton height="16px" /><Skeleton height="16px" width="60%" /></div>
  {:else if sso.status === 'unauthed'}
    <div class="notew"><span>You're signed out — sign in to manage single sign-on.</span></div>
  {:else if sso.status === 'forbidden'}
    <div class="comingsoon">
      <div class="chead"><b>Single sign-on (OIDC)</b></div>
      <p class="hint">Single sign-on is configured by the organization owner.</p>
    </div>
  {:else if sso.status === 'error'}
    <div class="notew"><span>Couldn't load SSO settings: {sso.error?.message}</span></div>
  {:else}
    <div class="settingsrow">
      <div>
        <b>Single sign-on (OIDC)</b>
        {#if sso.data.configured}
          <p class="hint">Your team signs in through your identity provider. New members are
            provisioned automatically the first time they sign in.</p>
        {:else}
          <p class="hint">Connect your identity provider (Okta, Google, Microsoft Entra…) so your
            team signs in with your company login, over OpenID Connect.</p>
        {/if}
      </div>
      <button class="btn small" onclick={openSSO}>{sso.data.configured ? 'Edit' : 'Set up SSO'}</button>
    </div>
    {#if sso.data.configured}
      <div class="ssometa">
        <div class="ssorow"><span class="k">Issuer</span><code class="mono">{sso.data.issuer}</code></div>
        <div class="ssorow"><span class="k">Client ID</span><code class="mono">{sso.data.client_id}</code></div>
        <div class="ssorow"><span class="k">Domains</span>
          <span class="dchips">{#each sso.data.domains as d}<Chip>{d}</Chip>{/each}</span></div>
        <div class="ssorow"><span class="k">Password login</span>
          <span>{sso.data.sso_required ? 'Disabled — SSO required for these domains' : 'Allowed alongside SSO'}</span></div>
      </div>
    {/if}

    <!-- SCIM 2.0 provisioning (W2-C2): base URL + shown-once bearer for the IdP's SCIM app. -->
    <div class="ssometa">
      <div class="settingsrow">
        <div>
          <b>Automated provisioning (SCIM)</b>
          <p class="hint">Let your identity provider create, update, and deactivate members
            automatically. Deactivating in your IdP revokes their gateway access within the request.
            {#if Object.keys(sso.data.scim_group_role_map ?? {}).length}
              IdP groups map to roles below (edit in “{sso.data.configured ? 'Edit' : 'Set up SSO'}”).{/if}</p>
        </div>
        {#if sso.data.scim_has_token}
          <button class="btn small danger" onclick={revokeScim} disabled={scimBusy}>Revoke token</button>
        {/if}
      </div>
      <div class="baseurl">
        <span class="bl">SCIM base URL</span>
        <code>{sso.data.scim_base_url}</code>
        <button class="btn small" type="button" onclick={() => copy(sso.data.scim_base_url, 'scimurl')}>{copied === 'scimurl' ? 'Copied' : 'Copy'}</button>
      </div>
      {#if scimToken}
        <div class="field">
          <label for="scim-secret">SCIM bearer token (paste into your IdP now)</label>
          <div class="secretrow">
            <code id="scim-secret" class="mono secret">{scimToken}</code>
            <button class="btn small" onclick={() => copy(scimToken, 'scimtok')}>{copied === 'scimtok' ? 'Copied' : 'Copy'}</button>
          </div>
          <p class="hint">Copy this now — it won't be shown again.</p>
        </div>
      {:else}
        <button class="btn small" onclick={genScimToken} disabled={scimBusy}>
          {scimBusy ? 'Working…' : sso.data.scim_has_token ? 'Rotate token' : 'Generate token'}
        </button>
      {/if}
      {#if sso.data.scim_group_role_map && Object.keys(sso.data.scim_group_role_map).length}
        <div class="ssorow"><span class="k">Group → role</span>
          <span class="dchips">{#each Object.entries(sso.data.scim_group_role_map) as [g, r]}<Chip>{g} → {r}</Chip>{/each}</span></div>
      {/if}
      {#if scimError}<div class="notew"><span>{scimError}</span></div>{/if}
    </div>
  {/if}
</Card>

<!-- ========================= DANGER ZONE ========================= -->
<Card title="Danger zone" class="dangercard">
  <div class="settingsrow">
    <div>
      <b>Delete organization</b>
      <p class="hint">Permanently remove this org, its teams, members, and usage history. Not reversible.</p>
    </div>
    <button class="btn danger" disabled title="Not available in this build">Delete organization</button>
  </div>
</Card>
{/if}

<Modal bind:open={renameOpen} title="Rename organization" subtitle="Visible everywhere the org name appears.">
  <div class="field">
    <label for="orgname-input">Organization name</label>
    <input
      id="orgname-input"
      bind:value={nameDraft}
      placeholder="Organization name"
      onkeydown={(e) => e.key === 'Enter' && saveRename()}
    />
  </div>
  {#if saveError}
    <div class="notew"><span>{saveError}</span></div>
  {/if}
  {#snippet footer()}
    <button class="btn ghost" onclick={() => (renameOpen = false)}>Cancel</button>
    <button class="btn primary" onclick={saveRename} disabled={saving || !nameDraft.trim()}>
      {saving ? 'Saving…' : 'Save'}
    </button>
  {/snippet}
</Modal>

<!-- Create API key: label -> mint -> the secret is shown ONCE, then gone. -->
<Modal bind:open={createOpen} title="Create API key" subtitle="Name it so you can recognize it later.">
  {#if newSecret}
    <div class="field">
      <label for="secret-out">Your new API key</label>
      <div class="secretrow">
        <code id="secret-out" class="mono secret">{newSecret}</code>
        <button class="btn small" onclick={() => copy(newSecret, 'secret')}>{copied === 'secret' ? 'Copied' : 'Copy'}</button>
      </div>
    </div>
    <div class="notew">
      <svg viewBox="0 0 24 24"><path d="M12 9v4M12 17h.01" /><path d="M10.3 3.9 2 18a2 2 0 0 0 1.7 3h16.6A2 2 0 0 0 22 18L13.7 3.9a2 2 0 0 0-3.4 0z" /></svg>
      <span>Copy this now — it won't be shown again.</span>
    </div>
  {:else}
    <div class="field">
      <label for="key-label">Label</label>
      <input
        id="key-label"
        bind:value={labelDraft}
        placeholder="e.g. laptop CLI"
        maxlength="100"
        onkeydown={(e) => e.key === 'Enter' && doMint()}
      />
    </div>
    {#if multiOrg}
      <div class="field">
        <label for="key-org">Organization</label>
        <select id="key-org" bind:value={orgDraft}>
          <option value="">Default (your primary org)</option>
          {#each orgs as m}<option value={m.org_id}>{m.org_name}</option>{/each}
        </select>
        <p class="hint">The key acts within this org — its policies, budgets, and audit apply to every request it makes.</p>
      </div>
    {/if}
    <div class="field">
      <label for="key-expiry">Expires after (days)</label>
      <input id="key-expiry" type="number" min="1" bind:value={expiryDraft} placeholder="Leave blank for no expiry" />
      <p class="hint">Your organization may cap this — a longer request is shortened to the cap.</p>
    </div>
    {#if mintError}<div class="notew"><span>{mintError}</span></div>{/if}
  {/if}
  {#snippet footer()}
    {#if newSecret}
      <button class="btn primary" onclick={() => (createOpen = false)}>Done</button>
    {:else}
      <button class="btn ghost" onclick={() => (createOpen = false)}>Cancel</button>
      <button class="btn primary" onclick={doMint} disabled={minting || !labelDraft.trim()}>
        {minting ? 'Creating…' : 'Create key'}
      </button>
    {/if}
  {/snippet}
</Modal>

<!-- Rotate API key: mint a new secret (shown ONCE); the old works for the grace window then dies. -->
<Modal bind:open={rotateOpen} title="Rotate API key"
       subtitle={rotateTarget ? `Replace the secret for "${rotateTarget.label}".` : ''}>
  {#if rotatedSecret}
    <div class="field">
      <label for="rot-out">Your new API key</label>
      <div class="secretrow">
        <code id="rot-out" class="mono secret">{rotatedSecret}</code>
        <button class="btn small" onclick={() => copy(rotatedSecret, 'rot')}>{copied === 'rot' ? 'Copied' : 'Copy'}</button>
      </div>
    </div>
    <div class="notew">
      <svg viewBox="0 0 24 24"><path d="M12 9v4M12 17h.01" /><path d="M10.3 3.9 2 18a2 2 0 0 0 1.7 3h16.6A2 2 0 0 0 22 18L13.7 3.9a2 2 0 0 0-3.4 0z" /></svg>
      <span>Copy this now — it won't be shown again. The old key keeps working briefly, then stops.</span>
    </div>
  {:else}
    <p class="hint">The old secret keeps working for your organization's rotation grace window, then
      stops. Update your client with the new secret before the window closes.</p>
    {#if rotateError}<div class="notew"><span>{rotateError}</span></div>{/if}
  {/if}
  {#snippet footer()}
    {#if rotatedSecret}
      <button class="btn primary" onclick={() => (rotateOpen = false)}>Done</button>
    {:else}
      <button class="btn ghost" onclick={() => (rotateOpen = false)}>Cancel</button>
      <button class="btn primary" onclick={doRotate} disabled={rotating}>
        {rotating ? 'Rotating…' : 'Rotate key'}
      </button>
    {/if}
  {/snippet}
</Modal>

<!-- Service token mint: org-owned CI credential, shown ONCE. Admin-gated server-side. -->
<Modal bind:open={svcOpen} title="Create service token"
       subtitle="An organization-owned key for CI — not tied to any person.">
  {#if svcSecret}
    <div class="field">
      <label for="svc-out">Your new service token</label>
      <div class="secretrow">
        <code id="svc-out" class="mono secret">{svcSecret}</code>
        <button class="btn small" onclick={() => copy(svcSecret, 'svc')}>{copied === 'svc' ? 'Copied' : 'Copy'}</button>
      </div>
    </div>
    <div class="notew">
      <svg viewBox="0 0 24 24"><path d="M12 9v4M12 17h.01" /><path d="M10.3 3.9 2 18a2 2 0 0 0 1.7 3h16.6A2 2 0 0 0 22 18L13.7 3.9a2 2 0 0 0-3.4 0z" /></svg>
      <span>Copy this now — it won't be shown again.</span>
    </div>
  {:else}
    <div class="field">
      <label for="svc-label">Label</label>
      <input id="svc-label" bind:value={svcLabel} placeholder="e.g. GitHub Actions" maxlength="100"
             onkeydown={(e) => e.key === 'Enter' && doSvcMint()} />
    </div>
    {#if svcError}<div class="notew"><span>{svcError}</span></div>{/if}
  {/if}
  {#snippet footer()}
    {#if svcSecret}
      <button class="btn primary" onclick={() => (svcOpen = false)}>Done</button>
    {:else}
      <button class="btn ghost" onclick={() => (svcOpen = false)}>Cancel</button>
      <button class="btn primary" onclick={doSvcMint} disabled={svcMinting || !svcLabel.trim()}>
        {svcMinting ? 'Creating…' : 'Create token'}
      </button>
    {/if}
  {/snippet}
</Modal>

<!-- Set / replace a provider key: pasted once, sent once, wiped on close. -->
<Modal
  bind:open={keyOpen}
  title={keyTarget ? `${keyTarget.replacing ? 'Replace' : 'Add'} ${keyTarget.label} key` : 'Add key'}
  subtitle="Paste the key from your provider dashboard."
>
  <div class="field">
    <label for="pk-input">API key</label>
    <input
      id="pk-input"
      type="password"
      autocomplete="new-password"
      bind:value={keyDraft}
      placeholder="sk-…"
      onkeydown={(e) => e.key === 'Enter' && saveKey()}
    />
  </div>
  <div class="notew">
    <svg viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2" /><path d="M7 11V7a5 5 0 0 1 10 0v4" /></svg>
    <span>Stored encrypted; never displayed again. Only the last 4 characters are kept as a hint.
      {#if keyTarget?.kind === 'governance'}It's verified with {keyTarget.label} before saving.{/if}</span>
  </div>
  {#if keyError}<div class="notew"><span>{keyError}</span></div>{/if}
  {#snippet footer()}
    <button class="btn ghost" onclick={() => (keyOpen = false)} disabled={keySaving}>Cancel</button>
    <button class="btn primary" onclick={saveKey} disabled={keySaving || !keyDraft.trim()}>
      {keySaving ? 'Saving…' : 'Save key'}
    </button>
  {/snippet}
</Modal>

<!-- Remove provider key confirm -->
<Modal
  bind:open={pkRemoveOpen}
  danger
  title="Remove provider key"
  subtitle={pkRemoveTarget ? `The stored ${pkRemoveTarget.label} key will be deleted.` : ''}
>
  <p class="hint">
    {#if pkRemoveTarget?.kind === 'inference'}
      Traffic falls back to members' personal keys or the platform's shared credentials.
    {:else}
      Governance dashboards for this provider will stop updating.
    {/if}
  </p>
  {#if pkRemoveError}<div class="notew"><span>{pkRemoveError}</span></div>{/if}
  {#snippet footer()}
    <button class="btn ghost" onclick={() => (pkRemoveOpen = false)} disabled={pkRemoving}>Cancel</button>
    <button class="btn danger" onclick={doRemoveKey} disabled={pkRemoving}>{pkRemoving ? 'Removing…' : 'Remove key'}</button>
  {/snippet}
</Modal>

<!-- Bulk revoke confirm — org-wide incident-response kill. Owner-gated server-side. -->
<Modal bind:open={bulkOpen} danger title="Revoke all tokens"
  subtitle="This affects everyone in your organization at once.">
  {#if bulkResult}
    <p class="hint">Done. The following credentials were revoked:</p>
    <ul class="bulkcounts">
      <li><b>{bulkResult.counts?.api ?? 0}</b> personal API key{(bulkResult.counts?.api ?? 0) === 1 ? '' : 's'}</li>
      {#if bulkSessions}<li><b>{bulkResult.counts?.session ?? 0}</b> active session{(bulkResult.counts?.session ?? 0) === 1 ? '' : 's'} (signed out)</li>{/if}
      {#if bulkService}<li><b>{bulkResult.counts?.service ?? 0}</b> service token{(bulkResult.counts?.service ?? 0) === 1 ? '' : 's'}</li>{/if}
    </ul>
  {:else}
    <p class="hint" style="color:var(--crit)">
      {#if bulkSessions}Everyone in the org gets signed out and{:else}Every personal API key in the org stops working and{/if}
      any tool using a revoked key must re-authenticate. This can’t be undone.
    </p>
    <label class="ssotoggle">
      <input type="checkbox" bind:checked={bulkOrgWide} />
      <span><b>Organization-wide</b> — apply to every member, not one person. (This screen only supports the org-wide kill.)</span>
    </label>
    <label class="ssotoggle">
      <input type="checkbox" bind:checked={bulkSessions} />
      <span><b>Sign everyone out</b> — also end active browser sessions, not just API keys.</span>
    </label>
    <label class="ssotoggle">
      <input type="checkbox" bind:checked={bulkService} />
      <span><b>Include service tokens</b> — also kill the org’s CI / automation tokens. Leave off to keep automation running.</span>
    </label>
    {#if bulkErr}<div class="notew"><span>{bulkErr}</span></div>{/if}
  {/if}
  {#snippet footer()}
    {#if bulkResult}
      <button class="btn primary" onclick={() => (bulkOpen = false)}>Done</button>
    {:else}
      <button class="btn ghost" onclick={() => (bulkOpen = false)} disabled={bulkRunning}>Cancel</button>
      <button class="btn danger" onclick={doBulkRevoke} disabled={bulkRunning || !bulkOrgWide}
        title={bulkOrgWide ? undefined : 'This screen only supports org-wide revoke'}>
        {bulkRunning ? 'Revoking…' : 'Revoke all tokens'}
      </button>
    {/if}
  {/snippet}
</Modal>

<!-- Configure SSO: issuer + client id/secret (write-only) + domains + require-SSO toggle. -->
<!-- Storage connector: Save (when enabling) is gated on a passing connection test of the exact
     fields being saved — a typo'd secret can't become the org's storage destination. -->
<Modal bind:open={stOpen} title="Connect your own bucket" subtitle="Saving switches your org's file storage to this bucket. Existing files stay readable.">
  <div class="field">
    <label for="st-endpoint">Endpoint URL</label>
    <input id="st-endpoint" bind:value={stDraft.s3_endpoint} placeholder="https://s3.us-east-1.amazonaws.com" />
    <p class="hint">Works with AWS S3, MinIO, Cloudflare R2, and anything else that speaks S3.</p>
  </div>
  <div class="field">
    <label for="st-bucket">Bucket</label>
    <input id="st-bucket" bind:value={stDraft.s3_bucket} placeholder="acme-toto-files" />
  </div>
  <div class="field">
    <label for="st-region">Region</label>
    <input id="st-region" bind:value={stDraft.s3_region} placeholder="us-east-1" />
  </div>
  <div class="field">
    <label for="st-access">Access key ID</label>
    <input id="st-access" bind:value={stDraft.s3_access_key} placeholder="AKIA…" />
  </div>
  <div class="field">
    <label for="st-secret">Secret access key</label>
    <input
      id="st-secret"
      type="password"
      autocomplete="new-password"
      bind:value={stDraft.s3_secret}
      placeholder={orgStorage.data?.has_s3_secret ? 'Leave blank to keep current secret' : 'Paste the secret access key'}
    />
    <p class="hint">Stored encrypted, never shown again.{#if orgStorage.data?.has_s3_secret} A secret is already saved — leave blank to keep it.{/if}</p>
  </div>
  <label class="ssotoggle">
    <input type="checkbox" bind:checked={stDraft.s3_force_path_style} />
    <span><b>Path-style addressing</b> — keep on for MinIO and most self-hosted stores; turn off for AWS.</span>
  </label>

  <div class="field">
    <div class="secretrow">
      <button class="btn small" type="button" onclick={testStorage} disabled={stTesting || !stComplete}>
        {stTesting ? 'Testing…' : 'Test connection'}
      </button>
      {#if stVerified}
        <span class="hint">Connection verified — a test file was written, read back, and deleted.</span>
      {:else if stTested && !stTested.ok}
        <span class="hint">Test failed: {stTested.error}</span>
      {:else if stTested}
        <span class="hint">Fields changed since the last test — test again.</span>
      {:else if !stComplete}
        <span class="hint">Fill in the endpoint, bucket, and secret to test.</span>
      {/if}
    </div>
  </div>
  {#if stError}<div class="notew"><span>{stError}</span></div>{/if}
  {#snippet footer()}
    <button class="btn ghost" onclick={() => (stOpen = false)} disabled={stSaving}>Cancel</button>
    <button class="btn primary" onclick={saveStorage} disabled={!stCanSave}
            title={!stVerified ? 'Run a passing connection test first' : undefined}>
      {stSaving ? 'Saving…' : 'Save & switch'}
    </button>
  {/snippet}
</Modal>

<Modal bind:open={ssoOpen} title="Single sign-on (OIDC)" subtitle="Your identity provider's OpenID Connect details.">
  <div class="field">
    <label for="sso-redirect">Redirect URL (add this to your IdP)</label>
    <div class="secretrow">
      <code id="sso-redirect" class="mono pkhint ssored">{ssoRedirectUri}</code>
      <button class="btn small" type="button" onclick={() => copy(ssoRedirectUri, 'ssored')}>{copied === 'ssored' ? 'Copied' : 'Copy'}</button>
    </div>
  </div>
  <div class="field">
    <label for="sso-issuer">Issuer URL</label>
    <input id="sso-issuer" bind:value={ssoDraft.issuer} placeholder="https://your-org.okta.com" />
  </div>
  <div class="field">
    <label for="sso-cid">Client ID</label>
    <input id="sso-cid" bind:value={ssoDraft.client_id} placeholder="0oa1b2c3d4…" />
  </div>
  <div class="field">
    <label for="sso-secret">Client secret</label>
    <input
      id="sso-secret"
      type="password"
      autocomplete="new-password"
      bind:value={ssoDraft.client_secret}
      placeholder={sso.data?.has_secret ? 'Leave blank to keep current secret' : 'Paste the client secret'}
    />
    <p class="hint">Stored encrypted, never shown again.{#if sso.data?.has_secret} A secret is already saved — leave blank to keep it.{/if}</p>
  </div>
  <div class="field">
    <label for="sso-domains">Email domains</label>
    <input id="sso-domains" bind:value={ssoDraft.domains} placeholder="acme.com, acme.io" />
    <p class="hint">Comma-separated. People with these email domains sign in through this provider.</p>
  </div>
  <label class="ssotoggle">
    <input type="checkbox" bind:checked={ssoDraft.sso_required} />
    <span><b>Require SSO</b> — turn off password sign-in for these domains.</span>
  </label>

  <div class="scimblock">
    <label class="ssotoggle">
      <input type="checkbox" bind:checked={ssoDraft.scim_enabled} />
      <span><b>SCIM provisioning</b> — let your IdP push member create/deactivate over SCIM 2.0.</span>
    </label>
    <div class="field">
      <label for="scim-map">Group → role mapping</label>
      {#each ssoDraft.scimRows as row, i}
        <div class="scimrow">
          <input placeholder="IdP group name (e.g. gateway-admins)" bind:value={row.group} />
          <select bind:value={row.role}>
            {#each SCIM_ROLES as r}<option value={r}>{r}</option>{/each}
          </select>
          <button class="btn small ghost" type="button" onclick={() => (ssoDraft.scimRows = ssoDraft.scimRows.filter((_, j) => j !== i))}>Remove</button>
        </div>
      {/each}
      <button class="btn small" type="button" onclick={() => (ssoDraft.scimRows = [...ssoDraft.scimRows, { group: '', role: 'member' }])}>Add mapping</button>
      <p class="hint">A member in several mapped groups gets the highest role. Unmapped groups
        default to member. Owner is never grantable via SCIM.</p>
    </div>
  </div>
  {#if ssoError}<div class="notew"><span>{ssoError}</span></div>{/if}
  {#snippet footer()}
    <button class="btn ghost" onclick={() => (ssoOpen = false)} disabled={ssoSaving}>Cancel</button>
    <button class="btn primary" onclick={saveSSO} disabled={ssoSaving || !ssoDraft.issuer.trim()}>
      {ssoSaving ? 'Saving…' : 'Save SSO'}
    </button>
  {/snippet}
</Modal>

<!-- Revoke confirm -->
<Modal
  bind:open={revokeOpen}
  danger
  title="Revoke API key"
  subtitle={revokeTarget ? `“${revokeTarget.label}” will stop working immediately.` : ''}
>
  <p class="hint">Any client using this key will start getting 401s. This can't be undone.</p>
  {#if revokeError}<div class="notew"><span>{revokeError}</span></div>{/if}
  {#snippet footer()}
    <button class="btn ghost" onclick={() => (revokeOpen = false)} disabled={revoking}>Cancel</button>
    <button class="btn danger" onclick={doRevoke} disabled={revoking}>{revoking ? 'Revoking…' : 'Revoke key'}</button>
  {/snippet}
</Modal>

<style>
  /* Layout only — colors/type/spacing tokens all come from app.css. */
  :global(.card + .card) { margin-top: var(--gap-block); }
  .settingsrow { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
  .orgname { font-size: 0.9375rem; font-weight: calc(650 + (var(--ui-weight) - 400)); letter-spacing: -0.01em; }
  .hint { margin: 3px 0 0; color: var(--text-3); font-size: 0.75rem; max-width: 46ch; }
  .comingsoon .chead { display: flex; align-items: center; gap: 9px; margin-bottom: 4px; }
  .comingsoon b { font-size: 0.9375rem; font-weight: calc(650 + (var(--ui-weight) - 400)); }

  /* SSO summary */
  .ssometa { margin-top: 14px; border-top: 1px solid var(--line); padding-top: 12px; display: flex; flex-direction: column; gap: 9px; }
  .ssorow { display: flex; align-items: baseline; gap: 12px; font-size: 0.8125rem; }
  .ssorow .k { flex: 0 0 108px; color: var(--text-3); font-size: 0.75rem; }
  .ssorow code { color: var(--text-2); overflow-x: auto; white-space: nowrap; }
  .dchips { display: flex; flex-wrap: wrap; gap: 6px; }
  .ssotoggle { display: flex; align-items: flex-start; gap: 9px; margin-top: 4px; font-size: 0.8125rem; color: var(--text-2); cursor: pointer; }
  .ssotoggle input { margin-top: 2px; flex: 0 0 auto; }
  .ssored { flex: 1 1 auto; }
  .scimblock { margin-top: 12px; border-top: 1px solid var(--line); padding-top: 12px; }
  .scimrow { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  .scimrow input { flex: 1 1 auto; }
  .scimrow select { flex: 0 0 auto; }
  :global(.dangercard .ch) { color: var(--crit); }
  .btn.danger:disabled { opacity: 0.55; cursor: not-allowed; filter: none; }
  .bulkzone { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
  .bulkcounts { margin: 8px 0 0; padding-left: 18px; font-size: 0.8125rem; color: var(--text-2); line-height: 1.7; }

  /* Provider keys */
  .pkgroup { margin-top: 14px; }
  .pkgroup + .pkgroup { border-top: 1px solid var(--line); padding-top: 14px; }
  .pkghead { font-size: 0.6875rem; letter-spacing: .08em; text-transform: uppercase; color: var(--text-3); font-weight: calc(600 + (var(--ui-weight) - 400)); }
  .pkrow { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; padding: 10px 0; }
  .pkrow + .pkrow { border-top: 1px solid var(--line); }
  .pkrow b { font-size: 0.875rem; font-weight: calc(600 + (var(--ui-weight) - 400)); }
  .pkactions { display: flex; align-items: center; gap: 8px; }
  .pkhint { font-size: 0.75rem; color: var(--text-2); background: var(--panel-2); border: 1px solid var(--line); border-radius: 6px; padding: 3px 7px; }

  /* API keys */
  .headbtn { margin-left: auto; }
  .baseurl { display: flex; align-items: center; gap: 10px; margin: 4px 0 14px; flex-wrap: wrap; }
  .baseurl .bl { font-size: 0.6875rem; letter-spacing: .08em; text-transform: uppercase; color: var(--text-3); font-weight: calc(600 + (var(--ui-weight) - 400)); }
  .baseurl code { font-size: 0.8125rem; color: var(--text-2); background: var(--panel-2); border: 1px solid var(--line); border-radius: 7px; padding: 5px 9px; }
  .skrows { display: flex; flex-direction: column; gap: 9px; padding: 4px 0; }
  .emptyk { color: var(--text-3); font-size: 0.8125rem; padding: 10px 0 4px; }
  .ta-r { text-align: right; }
  .policyrow { display: flex; align-items: flex-end; gap: 14px; flex-wrap: wrap; margin-top: 10px; }
  .policyrow .field { margin: 0; }
  .secretrow { display: flex; align-items: center; gap: 9px; }
  .secret { flex: 1 1 auto; overflow-x: auto; white-space: nowrap; background: var(--accent-soft); border: 1px solid var(--accent-line); border-radius: 8px; padding: 9px 11px; color: var(--accent); font-size: 0.8125rem; }
  .secretrow .btn { flex: 0 0 auto; }
</style>
