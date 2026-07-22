<script>
  // Provider connections (single-tenant Settings): paste your provider API keys and routing uses
  // them on the very next request — no env vars, no restart. Backed by /v1/admin/provider-keys
  // (toto_gateway/routes/provider_keys.py): keys are encrypted at rest server-side and NEVER
  // returned — rows only ever hold the masked last 4. An env-var key shows as an informational
  // "Set via environment" row; pasting a key here overrides it, Remove falls back to it.
  // Self-contained on purpose (client.js + shared primitives only, no admin.js edits) so it
  // mounts with a one-line import in settings/+page.svelte.
  import Card from '$lib/components/Card.svelte';
  import Skeleton from '$lib/components/Skeleton.svelte';
  import { get, put, del } from '$lib/api/client.js';
  import { query } from '$lib/api/resource.svelte.js';
  import { revealIn } from '$lib/motion.js';

  const keys = query(() => get('/v1/admin/provider-keys'));
  const rows = $derived(keys.data?.providers ?? []);
  // catalog_defaulted → a saved key recomposes the catalog live (its models appear with no restart).
  // false = the operator pinned TOTO_GW_CATALOG, so adding a provider means editing that env var.
  const catalogDefaulted = $derived(keys.data?.catalog_defaulted ?? true);

  // Per-provider key recipes — where to create the key, what scope it needs, what it usually
  // looks like. URLs and shapes verified against provider docs 2026-07-21; shapes are hints
  // ("usually"), never enforced — providers change formats. Fireworks publishes no key shape.
  const GUIDES = {
    openrouter: {
      url: 'https://openrouter.ai/keys',
      steps: ['Create Key — it’s shown once, copy it right away.'],
      shape: 'sk-or-v1-…',
    },
    fireworks: {
      url: 'https://app.fireworks.ai/settings/users/api-keys',
      steps: ['Account settings → API Keys → Create API key — shown once.'],
      shape: null,
    },
    cloudflare: {
      url: 'https://dash.cloudflare.com/profile/api-tokens',
      steps: [
        'Create Token → use the “Workers AI” template → Account Resources: Include your account.',
        'Account ID: the 32-character code in your dashboard URL after you pick your account (dash.cloudflare.com/<account-id>) — not your email.',
      ],
      shape: 'a 40-character code',
    },
    openai: {
      url: 'https://platform.openai.com/api-keys',
      steps: ['Create new secret key — shown once.'],
      shape: 'sk-proj-…',
    },
    gemini: {
      url: 'https://aistudio.google.com/apikey',
      steps: ['Google AI Studio → Create API key (uses your Google account).'],
      shape: 'AIza…',
    },
    anthropic: {
      url: 'https://console.anthropic.com/settings/keys',
      steps: ['Console → API keys → Create Key — shown once.'],
      shape: 'sk-ant-…',
    },
  };

  const CF_ACCOUNT_ID = /^[0-9a-f]{32}$/i;

  let editing = $state('');       // provider slug whose paste field is open ('' = none)
  let keyDraft = $state('');
  let accountDraft = $state('');
  let saving = $state(false);
  let saveError = $state('');
  let saveNote = $state('');      // transient success line after a save recomposes the catalog
  let confirmRemove = $state(''); // provider slug armed for the confirming second Remove click

  // Soft warning only — a key containing @ or whitespace is almost certainly a paste mistake,
  // but prefixes are never hard-enforced (providers change formats).
  const keyLooksWrong = $derived(/[@\s]/.test(keyDraft.trim()));
  // Hard rule (mirrored server-side): a Cloudflare account id is exactly 32 hex chars.
  const accountInvalid = $derived(
    accountDraft.trim() !== '' && !CF_ACCOUNT_ID.test(accountDraft.trim()));

  function openEdit(row) {
    editing = row.provider;
    keyDraft = '';
    accountDraft = '';
    saveError = '';
    saveNote = '';
    confirmRemove = '';
  }

  function closeEdit() {
    editing = '';
    keyDraft = '';      // the raw key never outlives the field
    accountDraft = '';
    saveError = '';
  }

  async function save(row) {
    const key = keyDraft.trim();
    const account = accountDraft.trim();
    if (!key || saving) return;
    if (row.account_env && !account) {
      saveError = 'Both the API token and the account ID are needed.';
      return;
    }
    if (row.account_env && !CF_ACCOUNT_ID.test(account)) {
      saveError = 'That doesn’t look like a Cloudflare account ID — it’s the 32-character code '
        + 'in your dashboard URL (dash.cloudflare.com/<account-id>), not your email.';
      return;
    }
    saving = true;
    saveError = '';
    try {
      const res = await put(`/v1/admin/provider-keys/${row.provider}`, { key, account_id: account });
      closeEdit();
      const n = res?.models_added?.length ?? 0;
      saveNote = n
        ? `${row.label} connected — ${n} model${n === 1 ? '' : 's'} added to your catalog.`
        : `${row.label} connected.`;
      await keys.reload();
    } catch (e) {
      saveError = e?.status === 503
        ? 'This deploy has no key-encryption secret configured — keys can’t be stored yet.'
        : e?.status === 403
          ? 'Only the operator can manage provider keys.'
          : (e?.message || 'Could not save the key.');
    } finally {
      saving = false;
    }
  }

  async function remove(row) {
    if (confirmRemove !== row.provider) {
      confirmRemove = row.provider; // first click arms, second click removes
      return;
    }
    confirmRemove = '';
    try {
      await del(`/v1/admin/provider-keys/${row.provider}`);
    } catch { /* 404 = already gone — the reload shows truth either way */ }
    await keys.reload();
  }
</script>

<Card title="Provider connections">
  <p class="hint">Connect the model providers you have accounts with. Keys are encrypted before
    they're stored and never shown again — only the last 4 characters are kept so you can
    recognize them. {#if catalogDefaulted}A pasted key takes effect immediately — its models join
    your catalog with no restart.{:else}A pasted key authenticates immediately, but your gateway
    pins an explicit catalog (<code class="mono">TOTO_GW_CATALOG</code>) — edit that variable to add
    a provider's models.{/if}</p>

  {#if saveNote}<div class="notew ok" in:revealIn><span>{saveNote}</span></div>{/if}

  {#if keys.status === 'loading'}
    <div class="skrows"><Skeleton height="16px" /><Skeleton height="16px" width="70%" /></div>
  {:else if keys.status === 'unauthed'}
    <div class="notew"><span>You're signed out — sign in to manage provider connections.</span></div>
  {:else if keys.status === 'forbidden'}
    <div class="notew"><span>Provider connections are managed with the operator credential.</span></div>
  {:else if keys.status === 'error'}
    <div class="notew"><span>Couldn't load provider connections: {keys.error?.message}</span></div>
  {:else}
    <div in:revealIn>
      {#each rows as row (row.provider)}
        <div class="pcrow" class:open={editing === row.provider}>
          <div class="pcmain">
            <div>
              <b>{row.label}</b>
              <p class="hint">{row.powers}</p>
            </div>
            <div class="pcactions">
              {#if row.source === 'stored'}
                <code class="mono pchint">•••• {row.masked || '????'}</code>
                <button class="btn small" onclick={() => (editing === row.provider ? closeEdit() : openEdit(row))}>Replace</button>
                <button class="btn small" onclick={() => remove(row)}>
                  {confirmRemove === row.provider ? 'Really remove?' : 'Remove'}
                </button>
              {:else if row.source === 'environment'}
                <span class="envnote">Set via environment <code class="mono">{row.env_var}</code></span>
                <button class="btn small" onclick={() => (editing === row.provider ? closeEdit() : openEdit(row))}>Override</button>
              {:else}
                <button class="btn small primary" onclick={() => (editing === row.provider ? closeEdit() : openEdit(row))}>Connect</button>
              {/if}
            </div>
          </div>

          {#if GUIDES[row.provider]}
            {@const g = GUIDES[row.provider]}
            <details class="pcguide">
              <summary>How to get this key</summary>
              <ol>
                <li>Create it at <a href={g.url} target="_blank" rel="noreferrer noopener"
                    class="mono">{g.url.replace('https://', '')}</a></li>
                {#each g.steps as step}<li>{step}</li>{/each}
                {#if g.shape}<li>It usually looks like <code class="mono">{g.shape}</code></li>{/if}
              </ol>
            </details>
          {/if}

          {#if editing === row.provider}
            <div class="pcedit">
              {#if row.source === 'environment'}
                <p class="hint">A key pasted here is stored encrypted and wins over the
                  <code class="mono">{row.env_var}</code> environment variable. Removing it later
                  falls back to the environment key.</p>
              {/if}
              <div class="pcfields">
                <div class="field">
                  <label for="pk-{row.provider}">{row.account_env ? 'API token' : 'API key'}</label>
                  <input
                    id="pk-{row.provider}"
                    type="password"
                    autocomplete="off"
                    bind:value={keyDraft}
                    placeholder={row.account_env ? 'Paste your API token' : 'Paste your API key'}
                    onkeydown={(e) => e.key === 'Enter' && save(row)}
                  />
                  {#if keyLooksWrong}
                    <p class="fieldwarn">This looks like an email address or contains spaces —
                      API keys usually don't.</p>
                  {/if}
                </div>
                {#if row.account_env}
                  <div class="field">
                    <label for="pk-acct-{row.provider}">Account ID</label>
                    <input
                      id="pk-acct-{row.provider}"
                      autocomplete="off"
                      class:invalid={accountInvalid}
                      bind:value={accountDraft}
                      placeholder="32-character code, e.g. c8c30db3…"
                      onkeydown={(e) => e.key === 'Enter' && save(row)}
                    />
                    {#if accountInvalid}
                      <p class="fieldwarn">A Cloudflare account ID is exactly 32 characters
                        (0-9, a-f) — find it in your dashboard URL:
                        dash.cloudflare.com/&lt;account-id&gt;.</p>
                    {/if}
                  </div>
                {/if}
                <div class="pceditbtns">
                  <button class="btn ghost small" onclick={closeEdit}>Cancel</button>
                  <button class="btn primary small" onclick={() => save(row)}
                          disabled={saving || !keyDraft.trim() || (row.account_env && !accountDraft.trim())}>
                    {saving ? 'Saving…' : 'Save'}
                  </button>
                </div>
              </div>
              {#if saveError}<div class="notew"><span>{saveError}</span></div>{/if}
            </div>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
</Card>

<style>
  .pcrow {
    padding: 12px 0;
    border-bottom: 1px solid var(--line);
  }
  .pcrow:last-child { border-bottom: none; padding-bottom: 4px; }
  .pcmain {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
  }
  .pcmain .hint { margin: 2px 0 0; }
  .pcactions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-shrink: 0;
  }
  .pchint { color: var(--muted); font-size: 13px; }
  .pcguide { margin-top: 4px; }
  .pcguide summary {
    color: var(--muted);
    font-size: 12.5px;
    cursor: pointer;
    width: fit-content;
  }
  .pcguide summary:hover { color: inherit; }
  .pcguide ol {
    margin: 6px 0 2px;
    padding-left: 22px;
    font-size: 12.5px;
    color: var(--muted);
  }
  .pcguide li { margin: 2px 0; }
  .pcguide a { color: inherit; }
  .fieldwarn { margin: 4px 0 0; font-size: 12px; color: var(--warn, #9a6700); }
  .pcfields input.invalid { border-color: var(--warn, #9a6700); }
  .envnote { color: var(--muted); font-size: 13px; }
  .envnote code { font-size: 12px; }
  .notew.ok { color: var(--good, #1a7f37); }
  .pcedit { margin-top: 10px; }
  .pcfields {
    display: flex;
    align-items: flex-end;
    gap: 10px;
    flex-wrap: wrap;
  }
  .pcfields .field { flex: 1 1 220px; margin: 0; }
  .pceditbtns { display: flex; gap: 8px; padding-bottom: 2px; }
</style>
