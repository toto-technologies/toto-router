<script>
  import '../app.css';
  import { page } from '$app/state';
  import { base } from '$app/paths';
  import { NAV_GROUPS } from '$lib/nav.js';
  import { toggleTheme } from '$lib/theme.js';
  import TuneMenu from '$lib/components/TuneMenu.svelte';
  import Modal from '$lib/components/Modal.svelte';
  import { pageIn } from '$lib/motion.js';
  import { query } from '$lib/api/resource.svelte.js';
  import { getOrg, getMe, getUsage, logout, getMemberships, setActiveOrg } from '$lib/api/admin.js';
  import Login from '$lib/components/Login.svelte';
  import CommandPalette from '$lib/components/CommandPalette.svelte';
  import { clearOperatorToken } from '$lib/oss-auth.js';
  import { fmtUsd } from '$lib/usage.js';

  let { children } = $props();

  // Inlined edition check (not $lib/edition.js) so edition branches fold at build time — vite.config.js `define`.
  // (OSS also seamless-launches: app.html consumes a `#token=…` fragment into the operator cookie
  // before hydration, so the identity probes below fire already-authenticated.)
  const OSS = typeof __EDITION__ !== 'undefined' && __EDITION__ === 'oss';

  let orgOpen = $state(false);
  let killOpen = $state(false);
  let searchEl = $state(null);

  // Live shell identity — loaded once, browser-only (query() guards), unauthed handled gracefully.
  // getOrg (/v1/admin/org) is enterprise-only; OSS skips it and the shell name falls back to 'Organization'.
  const org = query(() => getOrg(), { immediate: !OSS });
  const me = query(() => getMe());
  // Multi-org (W2-C1): the caller's memberships + which one is active. The switcher list renders
  // only when there's more than one; a single-org user sees just their org, no switch affordance.
  const memberships = query(() => getMemberships());
  const orgs = $derived(memberships.data?.memberships ?? []);
  const activeOrgId = $derived(memberships.data?.active_org_id ?? orgId);
  const multiOrg = $derived(orgs.length > 1);
  let switching = $state(false);

  async function switchOrg(id) {
    if (id === activeOrgId || switching) return;
    switching = true;
    try {
      await setActiveOrg(id);
      window.location.reload(); // reload so every org-scoped surface (usage, policies, catalog) refetches
    } catch {
      switching = false; // a 403/network error leaves the current org intact
    }
  }
  // Current billing period = calendar month to date; sum cost across the rollup rows.
  const monthStart = new Date(new Date().getFullYear(), new Date().getMonth(), 1).toISOString();
  const usage = query(() => getUsage({ start: monthStart }));

  const orgName = $derived(org.data?.name ?? 'Organization');
  const orgId = $derived(org.data?.org_id ?? '');
  const signedOut = $derived(me.status === 'unauthed');
  const userEmail = $derived(me.data?.is_operator ? '' : (me.data?.email ?? ''));
  const userLabel = $derived(
    signedOut ? 'Not signed in' : me.data?.is_operator ? 'Operator' : displayName(userEmail)
  );
  const spendReady = $derived(usage.status === 'ok' || usage.status === 'empty');
  const spend = $derived((usage.data?.rows ?? []).reduce((s, r) => s + (Number(r.cost_usd) || 0), 0));
  // No trace DB (TOTO_GW_TRACE_DB=off, or a deploy without one): the endpoint answers an honest
  // empty rollup with trace_db:false — show "not tracked" rather than a misleading "$0.00".
  const trackingOff = $derived(usage.data?.trace_db === false);

  /** email local-part -> a friendly display name ("alex.funk" -> "Alex Funk"). */
  function displayName(email) {
    const local = (email || '').split('@')[0];
    const name = local.split(/[.\-_]/).filter(Boolean)
      .map((w) => w[0].toUpperCase() + w.slice(1)).join(' ');
    return name || 'Signed in';
  }
  function initials(s) {
    const parts = (s || '').replace(/@.*/, '').split(/[.\-_ ]/).filter(Boolean);
    return ((parts[0]?.[0] ?? '') + (parts[1]?.[0] ?? '')).toUpperCase() || '—';
  }
  // Shared honest currency — sub-cent spend never hides as "$0" (see $lib/usage.js fmtUsd).
  const money = fmtUsd;

  const active = (href) => page.url.pathname === href || page.url.pathname.startsWith(href + '/');

  // Gate the whole shell on identity: only a resolved /v1/auth/me (200) renders the console.
  // 'loading' shows a brief boot state (also the prerender/SSR state); anything else (401 unauthed,
  // or a network error) shows the login screen — never the sidebar/pages to a signed-out visitor.
  const authed = $derived(me.status === 'ok');
  const booting = $derived(me.status === 'loading');

  async function signOut() {
    try {
      await logout(); // server expires the session AND operator cookies
    } catch {
      /* revoke is best-effort; clear the client either way by reloading to the gate */
    }
    clearOperatorToken(); // belt + braces for the OSS operator cookie (client-set, client-cleared)
    window.location.assign(base + '/');
  }

  function onKey(e) {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      searchEl?.focus();
    }
  }
</script>

<svelte:window onkeydown={onKey} onclick={() => (orgOpen = false)} />

{#if booting}
  <div class="bootgate"><span class="bootspin"></span></div>
{:else if !authed}
  <Login />
{:else}
<div class="app">
  <!-- ========================= SIDEBAR ========================= -->
  <aside class="rail">
    <div class="brand">
      <div class="glyph"></div>
      <div class="name"><b>TOTO</b> <span>Control</span></div>
      <span class="live" title="Gateway online"></span>
    </div>
    <nav class="nav">
      {#each NAV_GROUPS as group}
        <div class="grp">{group.label}</div>
        {#each group.items as item}
          <a class="navitem" class:on={active(base + item.href)} href={base + item.href}>
            <svg viewBox="0 0 24 24">{@html item.icon}</svg><span>{item.label}</span>
          </a>
        {/each}
      {/each}
    </nav>
    <div class="railfoot">
      <div class="orgcard">
        <div class="av">{initials(userEmail || userLabel)}</div>
        <div class="who"><b>{userLabel}</b><small>{userEmail || (signedOut ? 'Sign in to continue' : 'Operator credential')}</small></div>
        <button class="signout" title="Sign out" aria-label="Sign out" onclick={signOut}>
          <svg viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9" /></svg>
        </button>
      </div>
      <a class="updates" href="https://toto.tech" target="_blank" rel="noopener">Get release updates</a>
    </div>
  </aside>

  <!-- ========================= TOPBAR ========================= -->
  <header class="top">
    <div class="orgswitch">
      {#if OSS}
        <!-- Single-tenant: one org, no create-org endpoint — a static name, no switcher. -->
        <span class="orgbtn orgstatic"><span class="dot"></span>{orgName}</span>
      {:else}
      <button
        class="orgbtn"
        aria-haspopup="true"
        aria-expanded={orgOpen}
        onclick={(e) => { e.stopPropagation(); orgOpen = !orgOpen; }}
      >
        <span class="dot"></span>{orgName}
        <svg class="chev" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9l6 6 6-6" /></svg>
      </button>
      <div class="orgmenu" class:open={orgOpen} role="menu" onclick={(e) => e.stopPropagation()}>
        {#if multiOrg}
          {#each orgs as m}
            <div
              class="row"
              class:sel={m.org_id === activeOrgId}
              role="menuitemradio"
              aria-checked={m.org_id === activeOrgId}
              tabindex="0"
              title={m.org_id}
              onclick={() => switchOrg(m.org_id)}
              onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); switchOrg(m.org_id); } }}
            >
              <span class="dot" style="background:linear-gradient(150deg,var(--accent),#567a37);display:flex;align-items:center;justify-content:center;font-weight:700;font-size:0.5625rem;color:#12240e">{m.org_name?.[0]?.toUpperCase() ?? 'O'}</span>{m.org_name}<small class="orgrole">{m.role}</small>
            </div>
          {/each}
        {:else}
          <div class="row sel" role="menuitem" title={orgId}><span class="dot" style="background:linear-gradient(150deg,var(--accent),#567a37);display:flex;align-items:center;justify-content:center;font-weight:700;font-size:0.5625rem;color:#12240e">{orgName[0]?.toUpperCase() ?? 'O'}</span>{orgName}</div>
        {/if}
        <div class="sep"></div>
        <div class="row add" role="menuitem"><span class="dot" style="border:1px dashed var(--line-2);background:transparent;display:flex;align-items:center;justify-content:center">+</span>New organization</div>
      </div>
      {/if}
    </div>

    <CommandPalette
      bind:this={searchEl}
      placeholder={OSS ? 'Search models, catalog, usage…' : 'Search models, teams, audit…'} />

    <div class="topright">
      <div class="spendchip" title={trackingOff
        ? 'Usage tracking is off — no trace database is configured (TOTO_GW_TRACE_DB)'
        : `${orgName} spend this billing period (month to date)`}>
        <span class="lab">Spend</span>
        <span class="val">
          {#if trackingOff}<span>not tracked</span>
          {:else}<b>{spendReady ? money(spend) : '—'}</b><span>&nbsp;this month</span>{/if}
        </span>
      </div>
      <button class="iconbtn" title="Toggle light / dark" aria-label="Toggle theme" onclick={toggleTheme}>
        <svg viewBox="0 0 24 24"><path d="M12 3a9 9 0 1 0 9 9 7 7 0 0 1-9-9z" /></svg>
      </button>
      {#if !OSS}
        <button class="killbtn" onclick={() => (killOpen = true)}>
          <svg viewBox="0 0 24 24"><path d="M12 3v9M6.4 6.4a8 8 0 1 0 11.2 0" /></svg>Kill-switch
        </button>
      {/if}
      <div class="avatar" title={userEmail || userLabel}>{initials(userEmail || userLabel)}</div>
    </div>
  </header>

  <!-- ========================= MAIN ========================= -->
  <!-- Keyed on the route so each navigation re-mounts + animates (in:pageIn).
       in-only (no out) → no overlap, no reserved-space jank; brisk ~150ms. -->
  <main class="main">
    {#key page.url.pathname}
      <div class="page" in:pageIn>
        {@render children?.()}
      </div>
    {/key}
  </main>
</div>

<TuneMenu />

{#if !OSS}
<Modal bind:open={killOpen} danger title="Trigger kill-switch?" subtitle="Halts all model traffic for {orgName} immediately.">
  <div class="notew">
    <svg viewBox="0 0 24 24"><path d="M12 9v4M12 17h.01" /><path d="M10.3 3.9 2 18a2 2 0 0 0 1.7 3h16.6A2 2 0 0 0 22 18L13.7 3.9a2 2 0 0 0-3.4 0z" /></svg>
    <span>Every team loses access until re-enabled. Use only for a live incident.</span>
  </div>
  {#snippet footer()}
    <button class="btn ghost" onclick={() => (killOpen = false)}>Cancel</button>
    <button class="btn danger" onclick={() => (killOpen = false)}>Halt all traffic</button>
  {/snippet}
</Modal>
{/if}
{/if}

<style>
  /* Boot gate — shown while /v1/auth/me resolves (and as the prerender/SSR state). */
  .bootgate {
    height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--bg);
  }
  .bootspin {
    width: 26px;
    height: 26px;
    border-radius: 50%;
    border: 2px solid var(--line-2);
    border-top-color: var(--accent);
    animation: bootspin 0.7s linear infinite;
  }
  @keyframes bootspin {
    to {
      transform: rotate(360deg);
    }
  }
  /* Single-tenant org name — the switcher pill without its interactive affordances. */
  .orgstatic {
    cursor: default;
  }
  /* Role tag on each org-switcher row (W2-C1). Pushed to the right, muted. */
  .orgmenu .row .orgrole {
    margin-left: auto;
    font-size: 0.625rem;
    letter-spacing: 0.02em;
    text-transform: uppercase;
    color: var(--text-3);
  }
  .orgmenu .row[tabindex]:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
  }
  /* Sign-out affordance in the rail footer's user card. */
  .orgcard .signout {
    margin-left: auto;
    flex: 0 0 auto;
    width: 26px;
    height: 26px;
    border-radius: 6px;
    background: transparent;
    border: 1px solid transparent;
    color: var(--text-3);
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: background 0.13s ease, color 0.13s ease, border-color 0.13s ease;
  }
  .orgcard .signout:hover {
    background: var(--panel-hi);
    border-color: var(--line-2);
    color: var(--text);
  }
  .orgcard .signout svg {
    width: 15px;
    height: 15px;
    stroke: currentColor;
    fill: none;
    stroke-width: 1.7;
  }
</style>
