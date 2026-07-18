<script>
  // W6 · Teams & People (brief §4 W6) — counts line (invitations clause only when > 0), plus an
  // activity sentence naming up to two teams with REAL traffic in the last day (usage grouped by
  // team, joined to team names — omitted entirely when there's no honest signal). sm-only widget.
  import WidgetFrame from '../WidgetFrame.svelte';
  import Skeleton from '$lib/components/Skeleton.svelte';
  import { revealIn } from '$lib/motion.js';
  import { base } from '$app/paths';
  import { query } from '$lib/api/resource.svelte.js';
  import { getUsage, listTeams, listMembers, listInvitations } from '$lib/api/admin.js';
  import { uniquePeople, activeTeams, activitySentence } from '../org.js';

  let { range = '24h', size = 'sm' } = $props();

  const members = query(() => listMembers());
  const teams = query(() => listTeams());
  const invites = query(() => listInvitations(), { isEmpty: () => false }); // zero invites is fine, not "empty"
  // last-day team activity; empty/error here only drops the sentence, never the widget
  const teamUsage = query(
    () => getUsage({ groupBy: ['team'], start: new Date(Date.now() - 864e5).toISOString() }),
    { isEmpty: () => false }
  );

  const people = $derived(uniquePeople(members.data?.members ?? []));
  const teamCount = $derived((teams.data?.teams ?? []).length);
  const inviteCount = $derived((invites.data?.invitations ?? []).length);
  const sentence = $derived(
    teamUsage.status === 'ok'
      ? activitySentence(activeTeams(teamUsage.data?.rows ?? [], teams.data?.teams ?? []))
      : ''
  );

  const loading = $derived(members.status === 'loading' || teams.status === 'loading' || invites.status === 'loading');
  const gate = $derived(['unauthed', 'forbidden', 'error'].includes(members.status) ? members.status : null);
  const solo = $derived(people <= 1 && inviteCount === 0);
</script>

<WidgetFrame
  id="teams"
  title="Teams & people"
  href="/teams"
  linkLabel={!loading && !gate && solo ? 'Invite people' : 'Manage teams & people'}
>
  {#if loading}
    <div class="sk-stack"><Skeleton height="14px" /><Skeleton width="70%" height="12px" /></div>
  {:else if gate === 'unauthed'}
    <div class="deadend"><p>Sign in to see your organization's people.</p></div>
  {:else if gate === 'forbidden'}
    <div class="deadend"><p>Teams and people are org-wide — they need admin access.</p></div>
  {:else if gate === 'error'}
    <div class="deadend">
      <p>Couldn't load teams — {members.error?.message ?? 'unknown error'}.</p>
      <button class="btn small" onclick={() => members.reload()}>Retry</button>
    </div>
  {:else if solo}
    <div class="deadend" in:revealIn><p>It's just you so far. Invite your team to share the gateway.</p></div>
  {:else}
    <div in:revealIn>
      <div class="statline">
        <span><b class="num">{people}</b> {people === 1 ? 'person' : 'people'}</span>
        <span class="dot" aria-hidden="true">·</span>
        <span><b class="num">{teamCount}</b> {teamCount === 1 ? 'team' : 'teams'}</span>
        {#if inviteCount > 0}
          <span class="dot" aria-hidden="true">·</span>
          <a class="invchip" href="{base}/teams">
            <span class="num">{inviteCount}</span> {inviteCount === 1 ? 'invitation' : 'invitations'} waiting
          </a>
        {/if}
      </div>
      {#if sentence}<p class="acts">{sentence}</p>{/if}
    </div>
  {/if}
</WidgetFrame>

<style>
  .sk-stack { display: flex; flex-direction: column; gap: 10px; }
  .deadend { display: flex; flex-direction: column; align-items: flex-start; gap: 7px; padding: 4px 2px; }
  .deadend p { margin: 0; font-size: 0.78125rem; color: var(--text-2); }

  .statline { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; font-size: 0.8125rem; color: var(--text-2); }
  .statline b { color: var(--text); font-size: 0.9375rem; font-weight: calc(600 + (var(--ui-weight) - 400)); }
  .dot { color: var(--text-3); }
  .invchip { display: inline-flex; align-items: center; gap: 4px; font-size: 0.6875rem;
    font-weight: calc(600 + (var(--ui-weight) - 400)); padding: 2px 8px; border-radius: 20px;
    color: var(--warn); background: var(--warn-soft); text-decoration: none; }
  .invchip:hover { text-decoration: underline; }
  .acts { margin: 9px 0 0; font-size: 0.75rem; color: var(--text-2); }
</style>
