<script>
  // W8 · Recent Activity (brief §4 W8) — the audit feed as fully humanized sentences via the
  // org.js verb map: bold actor (email when the members join resolves it, "System" otherwise),
  // no `who` line, raw action string demoted to a title tooltip. 6 events lg / 4 sm.
  import WidgetFrame from '../WidgetFrame.svelte';
  import Skeleton from '$lib/components/Skeleton.svelte';
  import { revealIn } from '$lib/motion.js';
  import { query } from '$lib/api/resource.svelte.js';
  import { listAudit, listMembers } from '$lib/api/admin.js';
  import { humanAudit, auditTone, ago } from '../org.js';

  let { range = '24h', size = 'lg' } = $props();

  const audit = query(() => listAudit({ limit: 8 }));
  // actor ids → emails (best-effort garnish; the feed renders fine without it)
  const members = query(() => listMembers(), { isEmpty: () => false });
  const emailFor = $derived(
    Object.fromEntries((members.data?.members ?? []).map((m) => [m.user_id, m.email]))
  );
  const who = (uid) => (uid ? (emailFor[uid] ?? uid) : null);

  const events = $derived((audit.data?.events ?? []).slice(0, size === 'lg' ? 6 : 4));
</script>

<WidgetFrame id="activity" title="Recent activity" meta="audit" href="/audit" linkLabel="See full audit log">
  {#if audit.status === 'loading'}
    <div class="sk-stack">
      {#each Array(size === 'lg' ? 6 : 4) as _, i}<Skeleton width={i % 2 ? '78%' : '96%'} height="12px" />{/each}
    </div>
  {:else if audit.status === 'unauthed'}
    <div class="deadend"><p>Sign in to see the audit trail.</p></div>
  {:else if audit.status === 'forbidden'}
    <div class="deadend"><p>The audit trail needs admin access.</p></div>
  {:else if audit.status === 'error'}
    <div class="deadend">
      <p>Couldn't load the audit trail — {audit.error?.message ?? 'unknown error'}.</p>
      <button class="btn small" onclick={() => audit.reload()}>Retry</button>
    </div>
  {:else if audit.status === 'empty' || !events.length}
    <div class="deadend"><p>Nothing in the audit log yet. Admin actions get recorded here.</p></div>
  {:else}
    <div class="feed" in:revealIn>
      {#each events as e (e.id)}
        {@const t = auditTone(e.action)}
        <div class="ev" title={e.action}>
          <span class="ic {t}">
            {#if t === 'crit'}
              <svg viewBox="0 0 24 24"><path d="M12 3v9M6.4 6.4a8 8 0 1 0 11.2 0" /></svg>
            {:else if t === 'ok'}
              <svg viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5" /></svg>
            {:else}
              <svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h10" /></svg>
            {/if}
          </span>
          <div class="tx"><b>{who(e.user_id) ?? 'System'}</b> {humanAudit(e, who)}</div>
          <span class="t num">{ago(e.ts)}</span>
        </div>
      {/each}
    </div>
  {/if}
</WidgetFrame>

<style>
  .sk-stack { display: flex; flex-direction: column; gap: 10px; }
  .deadend { display: flex; flex-direction: column; align-items: flex-start; gap: 7px; padding: 4px 2px; }
  .deadend p { margin: 0; font-size: 0.8125rem; color: var(--text-2); }

  /* app.css .feed pads rows 15px for full-bleed cards; inside the widget body the card
     padding already exists, so rows sit flush */
  .feed .ev { padding-left: 0; padding-right: 0; }
  .feed .tx { align-self: center; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .feed .t { align-self: center; }
</style>
