<script>
  // Audit export (W2-C4) — the SIEM/compliance surface, homed on the Audit page. Configure where the
  // gateway ships its hash-chained JSONL audit batches (its own store and/or a customer S3 bucket),
  // the cadence, and retention; watch the chain; download a batch; run an export now. Admin-writable,
  // auditor-readable. Self-contained so the Audit page stays a thin host (import + one tag).
  //
  // The S3 secret is WRITE-ONLY, exactly like the SSO / storage secrets: GET carries has_s3_secret
  // only, PUT with a blank s3_secret keeps the stored one. Backend: routes/admin_audit_export.py.
  import { browser } from '$app/environment';
  import Card from '$lib/components/Card.svelte';
  import Chip from '$lib/components/Chip.svelte';
  import Toggle from '$lib/components/Toggle.svelte';
  import Table from '$lib/components/Table.svelte';
  import SegmentedControl from '$lib/components/SegmentedControl.svelte';
  import Skeleton from '$lib/components/Skeleton.svelte';
  import { query } from '$lib/api/resource.svelte.js';
  import { getAuditExport, putAuditExport, listAuditBatches, runAuditExport } from '$lib/api/admin.js';

  // Operator names an org via ?org_id=; a scoped admin is server-pinned (orgId '').
  const orgId = browser ? (new URLSearchParams(location.search).get('org_id') ?? '') : '';

  const cfg = query(() => getAuditExport(orgId || undefined));
  const batches = query(() => listAuditBatches(orgId || undefined), { isEmpty: (d) => !d?.batches?.length });

  const DEST_OPTS = [
    { value: 'gateway', label: 'Gateway store' },
    { value: 's3', label: 'Your S3 bucket' },
    { value: 'both', label: 'Both' },
  ];
  const DESTHINT = {
    gateway: 'Batches are kept in the gateway’s own object store — downloadable from the table below.',
    s3: 'Batches are shipped to your bucket for your SIEM to ingest. They are not retained on the gateway.',
    both: 'Batches are kept on the gateway AND shipped to your bucket.',
  };

  // Draft mirrors the loaded config. Seeds whenever cfg.data changes reference (first load + each
  // post-save reload); reads only cfg.data, so in-progress edits are never clobbered.
  let d = $state(null);
  $effect(() => {
    const c = cfg.data;
    if (!c) return;
    d = {
      enabled: c.enabled, cadence_hours: c.cadence_hours, retention_days: c.retention_days,
      destination: c.destination, s3_endpoint: c.s3_endpoint, s3_bucket: c.s3_bucket,
      s3_region: c.s3_region, s3_access_key: c.s3_access_key, s3_prefix: c.s3_prefix, s3_secret: '',
    };
  });
  const needsS3 = $derived(d?.destination === 's3' || d?.destination === 'both');

  let saving = $state(false);
  let saveErr = $state('');
  let running = $state(false);
  let runMsg = $state('');

  async function save() {
    saving = true; saveErr = ''; runMsg = '';
    try {
      // Omit s3_secret when blank so the stored secret is kept (write-only contract).
      const { s3_secret, ...rest } = d;
      await putAuditExport({ ...rest, ...(s3_secret.trim() ? { s3_secret: s3_secret.trim() } : {}) }, orgId || undefined);
      await cfg.reload();
    } catch (e) {
      saveErr = e?.status === 403 ? 'Only an org owner or admin can configure audit export.'
        : e?.status === 503 ? 'This deploy has no credential-encryption secret — the S3 secret can’t be stored yet.'
        : (e?.message || 'Could not save audit-export config.');
    } finally { saving = false; }
  }

  async function runNow() {
    running = true; saveErr = ''; runMsg = '';
    try {
      const s = await runAuditExport(orgId || undefined);
      // summary.streams = [{stream, batch?, rows}]; a written stream carries a `batch` number.
      const streams = Array.isArray(s?.streams) ? s.streams : [];
      const batches = streams.filter((x) => x.batch != null).length;
      const rows = streams.reduce((a, x) => a + (x.rows ?? 0), 0);
      runMsg = batches
        ? `Exported ${rows} event${rows === 1 ? '' : 's'} in ${batches} batch${batches === 1 ? '' : 'es'}.`
        : 'Ran — no new events to export.';
      await Promise.all([cfg.reload(), batches.reload()]);
    } catch (e) {
      saveErr = e?.status === 404 ? 'Configure and save audit export before running it.'
        : (e?.message || 'Export run failed.');
    } finally { running = false; }
  }

  const short = (h) => (h ? h.slice(0, 12) : '—');
  const streamLabel = { gateway_events: 'Gateway events', audit_events: 'Audit events' };
  function fmtTime(ts) {
    return new Date(ts * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }
  function downloadHref(b) {
    return orgId ? `${b.download}?org_id=${encodeURIComponent(orgId)}` : b.download;
  }
</script>

<Card title="Audit export" meta="org-wide · SIEM / compliance">
  {#if cfg.status === 'loading'}
    <div style="display:flex;flex-direction:column;gap:10px"><Skeleton height="34px" /><Skeleton width="60%" height="34px" /></div>
  {:else if cfg.status === 'unauthed'}
    <div class="notew"><span>You’re signed out — sign in to configure audit export.</span></div>
  {:else if cfg.status === 'forbidden'}
    <div class="notew"><span>Audit export is configurable by org admins and owners only.</span></div>
  {:else if cfg.status === 'error'}
    <div class="notew"><span>Couldn’t load audit export: {cfg.error?.message || 'something went wrong.'}</span></div>
  {:else if d}
    <p class="hint" style="margin-top:0">Ships an append-only, hash-chained JSONL copy of your audit and
      gateway-event streams to your SIEM or object store on a schedule — the evidence a compliance
      auditor verifies against.</p>

    <div class="aerow">
      <div><b>Enabled</b><p class="hint">When on, the scheduler exports on the cadence below.</p></div>
      <Toggle bind:checked={d.enabled} label="Audit export enabled" />
    </div>

    <div class="aegrid">
      <div class="field">
        <label for="ae-cadence">Cadence (hours)</label>
        <input id="ae-cadence" type="number" min="1" step="1" bind:value={d.cadence_hours} />
      </div>
      <div class="field">
        <label for="ae-retention">Gateway retention (days, 0 = keep forever)</label>
        <input id="ae-retention" type="number" min="0" step="1" bind:value={d.retention_days} />
      </div>
    </div>

    <div class="aerow" style="align-items:flex-start">
      <div><b>Destination</b><p class="hint">{DESTHINT[d.destination]}</p></div>
      <SegmentedControl options={DEST_OPTS} bind:value={d.destination} />
    </div>

    {#if needsS3}
      <div class="s3box">
        <div class="eyebrow" style="margin-bottom:10px">Customer S3 bucket</div>
        <div class="aegrid">
          <div class="field">
            <label for="ae-endpoint">Endpoint URL</label>
            <input id="ae-endpoint" bind:value={d.s3_endpoint} placeholder="https://s3.us-east-1.amazonaws.com" />
          </div>
          <div class="field">
            <label for="ae-bucket">Bucket</label>
            <input id="ae-bucket" bind:value={d.s3_bucket} placeholder="acme-audit-export" />
          </div>
          <div class="field">
            <label for="ae-region">Region</label>
            <input id="ae-region" bind:value={d.s3_region} placeholder="us-east-1" />
          </div>
          <div class="field">
            <label for="ae-prefix">Key prefix (optional)</label>
            <input id="ae-prefix" bind:value={d.s3_prefix} placeholder="toto/audit/" />
          </div>
          <div class="field">
            <label for="ae-access">Access key ID</label>
            <input id="ae-access" bind:value={d.s3_access_key} placeholder="AKIA…" />
          </div>
          <div class="field">
            <label for="ae-secret">Secret access key</label>
            <input id="ae-secret" type="password" autocomplete="new-password" bind:value={d.s3_secret}
              placeholder={cfg.data?.has_s3_secret ? 'Leave blank to keep current secret' : 'Paste the secret access key'} />
          </div>
        </div>
        <p class="hint">Stored encrypted, never shown again.{#if cfg.data?.has_s3_secret} A secret is already saved — leave blank to keep it.{/if}</p>
      </div>
    {/if}

    <div class="aefoot">
      <div class="aestatus">
        {#if cfg.data?.last_error}
          <Chip variant="ovr" dot>last run failed</Chip>
          <span class="hint" style="color:var(--crit)">{cfg.data.last_error}</span>
        {:else if cfg.data?.last_run}
          <Chip variant="perim" dot>healthy</Chip>
          <span class="hint">Last run {fmtTime(cfg.data.last_run)}.</span>
        {:else}
          <span class="hint">Never run yet.</span>
        {/if}
        {#if runMsg}<span class="hint" style="color:var(--accent)">{runMsg}</span>{/if}
        {#if saveErr}<span class="hint" style="color:var(--crit)">{saveErr}</span>{/if}
      </div>
      <div class="aebtns">
        <button class="btn small" onclick={runNow} disabled={running || saving || !cfg.data?.configured}
          title={cfg.data?.configured ? undefined : 'Save the config first'}>{running ? 'Running…' : 'Run now'}</button>
        <button class="btn small primary" onclick={save} disabled={saving || running}>{saving ? 'Saving…' : 'Save'}</button>
      </div>
    </div>
  {/if}
</Card>

{#if cfg.status === 'ok' || cfg.status === 'empty'}
  <Card title="Exported batches" meta="hash-chain · newest first" class="aebatches">
    <div class="reltip">
      <svg viewBox="0 0 24 24"><path d="M12 8v5M12 16h.01" /><circle cx="12" cy="12" r="9" /></svg>
      <div>Each batch is fingerprinted with SHA-256, and every fingerprint folds in the previous
        batch’s — so the batches form a tamper-evident chain. Download a batch and recompute its
        SHA-256 to confirm it matches; a mismatch or a broken link means a batch was altered or dropped.</div>
    </div>
    {#if batches.status === 'loading'}
      <div style="margin-top:12px"><Skeleton height="30px" /></div>
    {:else if batches.status === 'empty'}
      <div class="emptyk" style="margin-top:12px">No batches exported yet. Enable export and run it, or wait for the next scheduled cycle.</div>
    {:else if batches.status === 'ok'}
      <Table>
        {#snippet head()}
          <tr><th>Stream</th><th>Batch</th><th>Rows</th><th>SHA-256</th><th>Prev</th><th>Created</th><th></th></tr>
        {/snippet}
        {#each batches.data.batches as b (b.stream + '/' + b.batch)}
          <tr>
            <td>{streamLabel[b.stream] ?? b.stream}</td>
            <td class="mono">#{b.batch}</td>
            <td class="muted">{b.rows}</td>
            <td class="mono muted" title={b.sha256}>{short(b.sha256)}</td>
            <td class="mono muted" title={b.prev_sha256}>{short(b.prev_sha256)}</td>
            <td class="muted">{fmtTime(b.created_at)}</td>
            <td class="ta-r"><a class="btn small" href={downloadHref(b)} download>Download</a></td>
          </tr>
        {/each}
      </Table>
    {:else if batches.status === 'error'}
      <div class="notew" style="margin-top:12px"><span>Couldn’t load batches: {batches.error?.message}</span></div>
    {/if}
  </Card>
{/if}

<style>
  .aerow { display: flex; align-items: center; justify-content: space-between; gap: 16px;
    padding: 12px 0; border-top: 1px solid var(--line); }
  .aerow b { font-size: 0.8125rem; font-weight: calc(600 + (var(--ui-weight) - 400)); }
  .aerow p { margin: 3px 0 0; }
  .aegrid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 16px; margin: 4px 0 8px; }
  .s3box { margin-top: 8px; padding: 14px; border: 1px solid var(--line); border-radius: 10px; background: var(--panel-2); }
  .aefoot { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;
    margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--line); }
  .aestatus { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0; }
  .aebtns { display: flex; gap: 8px; margin-left: auto; }
  .aebatches { margin-top: 14px; }
  @media (max-width: 640px) { .aegrid { grid-template-columns: 1fr; } }
</style>
