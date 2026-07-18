<script>
  // Standalone provenance manifest for one bespoke model — the audit answer to "why does this
  // model behave the way it does?". Fetches the full lineage (/v1/admin/tuning/lineage, enriched
  // with the owning credential scope + live serving state) and lays it out in four plain-language
  // panels: training data, the training job, the eval scoreboard, and ownership/serving.
  import { query } from '$lib/api/resource.svelte.js';
  import { getTuningLineage } from '$lib/api/admin.js';
  import {
    shortRef,
    pct,
    money,
    fmtTokens,
    fmtDuration,
    sortEvals,
    methodLabel,
    parseHyper,
  } from '$lib/tuning.js';
  import { fmtTime } from '$lib/time.js';
  import Skeleton from '$lib/components/Skeleton.svelte';

  // modelId: tuning model version id (e.g. 'docx-formatting-editor-v1'). orgId: the org whose
  // registry to read (operator must name one; a scoped admin can omit — server infers).
  let { modelId, orgId = undefined } = $props();

  const q = query(() => getTuningLineage(modelId, orgId || undefined), { isEmpty: () => false });

  const m = $derived(q.data ?? {});
  const evals = $derived(sortEvals(m.evals));
  const hyper = $derived(parseHyper(m.hyperparams));
  const serving = $derived(m.serving_state ?? {});
</script>

<div class="lcard">
  {#if q.status === 'loading'}
    <div class="skwrap">
      <Skeleton height="16px" width="40%" />
      <Skeleton /><Skeleton width="80%" /><Skeleton width="60%" />
    </div>
  {:else if q.status === 'unauthed' || q.status === 'forbidden'}
    <p class="quiet">Your role can’t view this model’s provenance.</p>
  {:else if q.status === 'error'}
    <p class="quiet">Couldn’t load provenance — {q.error?.message ?? 'unknown error'}.</p>
  {:else}
    <div class="lgrid">
      <!-- Training data -->
      <section>
        <h4>Training data</h4>
        <dl>
          <dt>Dataset</dt><dd class="n">{m.dataset_id ?? '—'}</dd>
          <dt>Generator</dt><dd>{m.generator || '—'}</dd>
          <dt>Seed</dt><dd class="n">{m.seed ?? '—'}</dd>
          <dt>Examples</dt><dd class="n">{m.train_examples ?? 0} train · {m.eval_examples ?? 0} eval</dd>
          <dt>Tokens</dt><dd class="n">{fmtTokens(m.train_tokens)}</dd>
          {#if m.source_manifest}<dt>Source</dt><dd class="n">{m.source_manifest}</dd>{/if}
        </dl>
      </section>

      <!-- Training job -->
      <section>
        <h4>Training job</h4>
        <dl>
          <dt>Job</dt><dd class="n">{m.job_id ?? '—'} · {m.state ?? '—'}</dd>
          <dt>Method</dt><dd>{methodLabel(m.method)}</dd>
          <dt>Base model</dt><dd class="n">{shortRef(m.base_model)}</dd>
          <dt>Cost</dt><dd class="n">{money(m.cost_actual_usd ?? m.cost_estimate_usd)}</dd>
          <dt>Duration</dt><dd class="n">{fmtDuration(m.job_created_at, m.job_completed_at)}</dd>
          {#if Object.keys(hyper).length}
            <dt>Hyperparams</dt>
            <dd class="n hp">{#each Object.entries(hyper) as [k, v] (k)}<span>{k}={v}</span>{/each}</dd>
          {/if}
        </dl>
      </section>

      <!-- Evals -->
      <section>
        <h4>Evaluations</h4>
        {#if evals.length}
          <table class="evals">
            <thead><tr><th>Run</th><th>n</th><th>match</th><th>valid</th><th>applied</th></tr></thead>
            <tbody>
              {#each evals as e (e.id)}
                <tr>
                  <td>{e.label || shortRef(e.model_ref)}</td>
                  <td class="n">{e.n ?? '—'}</td>
                  <td class="n b">{pct(e.match_rate)}</td>
                  <td class="n">{pct(e.valid_rate)}</td>
                  <td class="n">{pct(e.applied_rate)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        {:else}
          <p class="quiet">No eval runs recorded.</p>
        {/if}
      </section>

      <!-- Ownership + serving -->
      <section>
        <h4>Ownership &amp; serving</h4>
        <dl>
          <dt>Serving</dt>
          <dd>
            {#if serving.deployed}
              <span class="chip good"><span class="d"></span>Live in catalog</span>
            {:else}
              <span class="chip"><span class="d"></span>Training only — no live deployment</span>
            {/if}
          </dd>
          <dt>Credential scope</dt>
          <dd class="n">{serving.credential_scope_label ?? (serving.deployed ? 'platform (shared)' : '—')}</dd>
          {#if serving.provider}<dt>Provider</dt><dd>{serving.provider}</dd>{/if}
          {#if serving.upstream_model}<dt>Upstream</dt><dd class="n">{serving.upstream_model}</dd>{/if}
          {#if serving.catalog_id ?? m.catalog_id}<dt>Catalog id</dt><dd class="n">{m.catalog_id}</dd>{/if}
          {#if serving.source}<dt>Defined in</dt><dd class="n">{serving.source}</dd>{/if}
          {#if m.model_created_at}<dt>Created</dt><dd class="n">{fmtTime(m.model_created_at)}</dd>{/if}
        </dl>
      </section>
    </div>
  {/if}
</div>

<style>
  .lcard { padding: 4px 2px; }
  .skwrap { display: flex; flex-direction: column; gap: 10px; }
  .lgrid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 18px 28px;
  }
  section { min-width: 0; }
  h4 {
    margin: 0 0 8px;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-3);
    font-weight: calc(650 + (var(--ui-weight) - 400));
  }
  dl { display: grid; grid-template-columns: auto 1fr; gap: 4px 12px; margin: 0; }
  dt { color: var(--text-2); font-size: 0.8rem; white-space: nowrap; }
  dd { margin: 0; font-size: 0.82rem; color: var(--text); min-width: 0; overflow-wrap: anywhere; }
  dd.n { font-family: var(--mono); font-size: 0.78rem; }
  .hp { display: flex; flex-wrap: wrap; gap: 4px 10px; }
  .quiet { color: var(--text-3); font-size: 0.82rem; margin: 2px 0; }
  table.evals { width: 100%; border-collapse: collapse; font-size: 0.78rem; }
  table.evals th {
    text-align: right;
    color: var(--text-3);
    font-weight: 500;
    padding: 2px 6px;
    font-size: 0.72rem;
  }
  table.evals th:first-child, table.evals td:first-child { text-align: left; }
  table.evals td { padding: 3px 6px; border-top: 1px solid var(--line); }
  table.evals td.n { font-family: var(--mono); text-align: right; }
  table.evals td.b { color: var(--text); font-weight: 600; }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 0.76rem;
    padding: 2px 8px;
    border-radius: 999px;
    border: 1px solid var(--line);
    color: var(--text-3);
  }
  .chip .d { width: 6px; height: 6px; border-radius: 50%; background: var(--text-3); }
  .chip.good { color: var(--accent); border-color: var(--accent-line); }
  .chip.good .d { background: var(--accent); }
</style>
