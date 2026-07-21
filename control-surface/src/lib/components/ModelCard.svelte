<script>
  // One discovery-library model card — the locked v18 anatomy: hue top border, 30px logo tile,
  // 16px name + VIA eyebrow, centered labeled stats (price/context on OpenRouter, context/
  // fine-tuning on Fireworks), quiet caps line + cbox state left, ROUTES stack / Adopt right.
  import { prettyModel, providerLabel } from '$lib/models.js';
  import { logoFor } from '$lib/logos.js';
  import {
    vendorFromSlug,
    vendorHue,
    catMark,
    capsLine,
    routedTasks,
    perM,
    ctxShort,
  } from '$lib/catalog.js';

  // routing: the org routing-policy view or null (footer-right stays empty when null).
  // onadopt: called with the model on Add to Catalog; omit to hide the button.
  // onremove: called on Remove — only offered when m.adopted (added by this scope, not shipped).
  // ondetails: called when the card header is clicked (populates the details drawer).
  // busy: an adoption call for THIS model is in flight — buttons disable and say so.
  let { m, source, routing = null, onadopt = null, onremove = null, ondetails = null, busy = false } = $props();

  const vendor = $derived(vendorFromSlug(m.slug));
  const caps = $derived(capsLine(m, source));
  const tasks = $derived(m.cataloged && routing ? routedTasks(m.catalog_id, routing) : null);
</script>

<div class="mcard" style="--ph:{vendorHue(vendor)}">
  {#snippet header()}
    <span class="pmark" aria-hidden="true">
      {#if logoFor(vendor)}{@html logoFor(vendor)}{:else}{catMark(vendor)}{/if}
    </span>
    <div class="mcid">
      <div class="mcname">{m.name || prettyModel(m.slug)}</div>
      <div class="mcvia">via {providerLabel(source)} · {providerLabel(vendor)}</div>
    </div>
  {/snippet}
  {#if ondetails}
    <button class="mctop" onclick={() => ondetails(m)} title="Details">{@render header()}</button>
  {:else}
    <div class="mctop">{@render header()}</div>
  {/if}
  <div class="mcstats">
    {#if source === 'fireworks'}
      <!-- the platform API exposes no prices — context + the factory angle instead -->
      <div class="stat">
        <div class="v n">{ctxShort(m.context_window)}</div>
        <div class="l">context</div>
      </div>
      <div class="stat">
        <div class="v">{m.tunable ? 'Tunable · LoRA' : '—'}</div>
        <div class="l">fine-tuning</div>
      </div>
    {:else if source === 'cloudflare'}
      <!-- Cloudflare's models API exposes no per-token price — context + tool capability instead -->
      <div class="stat">
        <div class="v n">{ctxShort(m.context_window)}</div>
        <div class="l">context</div>
      </div>
      <div class="stat">
        <div class="v">{m.tools ? 'Function calling' : '—'}</div>
        <div class="l">tools</div>
      </div>
    {:else}
      <div class="stat">
        <div class="v n">{perM(m.price_in)} · {perM(m.price_out)}</div>
        <div class="l">per M in · out</div>
      </div>
      <div class="stat">
        <div class="v n">{ctxShort(m.context_window)}</div>
        <div class="l">context</div>
      </div>
    {/if}
  </div>
  <div class="mcfoot">
    <div class="fleft">
      {#if caps}<div class="caps">{caps}</div>{/if}
      <div class="frow">
        {#if m.cataloged}
          <span class="cbox on" title="In catalog" role="img" aria-label="In catalog">
            <svg width="10" height="10" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m2 6.2 2.7 2.7L10 3.3" /></svg>
          </span>
          <span class="mcfootid n">{m.catalog_id ?? ''}</span>
          {#if m.adopted}
            <span class="added" title="Added by you — not part of the shipped catalog">added</span>
            {#if onremove}
              <button class="rmbtn" disabled={busy} onclick={() => onremove(m)}>
                {busy ? 'Removing…' : 'Remove'}
              </button>
            {/if}
          {/if}
        {:else}
          <span class="cbox" title="Not in catalog" role="img" aria-label="Not in catalog"></span>
        {/if}
      </div>
    </div>
    {#if m.cataloged}
      {#if tasks}
        {#if tasks.length}
          <div class="tasklist">
            <span class="tl">routes</span>
            {#each tasks.slice(0, 3) as t (t)}<span>{t}</span>{/each}
            {#if tasks.length > 3}<span>+{tasks.length - 3} more</span>{/if}
          </div>
        {:else}
          <span class="notasks">no tasks routed</span>
        {/if}
      {/if}
    {:else if onadopt}
      <button class="btn small primary" disabled={busy} onclick={() => onadopt(m)}>
        {busy ? 'Adding…' : 'Add to Catalog'}
      </button>
    {/if}
  </div>
</div>

<style>
  .mcard {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 10px;
    box-shadow: var(--shadow);
    border-top: 3px solid light-dark(hsl(var(--ph) 48% 55%), hsl(var(--ph) 50% 62%));
    padding: 14px 14px 12px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-width: 0;
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
    letter-spacing: 0.02em;
    color: light-dark(hsl(var(--ph) 58% 30%), hsl(var(--ph) 52% 74%));
    background: light-dark(hsl(var(--ph) 55% 52% / 0.13), hsl(var(--ph) 50% 62% / 0.16));
    border: 1px solid light-dark(hsl(var(--ph) 48% 44% / 0.4), hsl(var(--ph) 52% 66% / 0.36));
  }
  .pmark :global(svg.plogo) {
    width: 16px;
    height: 16px;
    display: block;
  }
  .mctop {
    display: flex;
    align-items: center;
    gap: 12px;
    min-width: 0;
  }
  /* header-as-details-button: unstyled, name underlines on hover as the affordance */
  button.mctop {
    background: none;
    border: 0;
    padding: 0;
    font: inherit;
    color: inherit;
    text-align: left;
    cursor: pointer;
    width: 100%;
  }
  button.mctop:hover .mcname,
  button.mctop:focus-visible .mcname {
    text-decoration: underline;
    text-underline-offset: 3px;
    text-decoration-color: var(--text-3);
  }
  .mcid {
    min-width: 0;
  }
  .mcname {
    font-size: 1rem;
    font-weight: calc(640 + (var(--ui-weight) - 400));
    letter-spacing: -0.012em;
    line-height: 1.2;
    color: var(--text);
  }
  .mcvia {
    font-size: 0.625rem;
    letter-spacing: 0.13em;
    text-transform: uppercase;
    color: var(--text-3);
    font-weight: calc(600 + (var(--ui-weight) - 400));
    margin-top: 3px;
  }
  .mcstats {
    display: flex;
    gap: 22px;
    padding: 2px 0;
    justify-content: center;
    text-align: center;
  }
  .mcstats .v {
    font-size: 0.875rem;
    font-weight: calc(600 + (var(--ui-weight) - 400));
    color: var(--text);
  }
  .mcstats .l {
    font-size: 0.625rem;
    letter-spacing: 0.09em;
    text-transform: uppercase;
    color: var(--text-3);
    font-weight: calc(600 + (var(--ui-weight) - 400));
    margin-top: 2px;
  }
  .mcfoot {
    flex: 1;
    margin-top: auto;
    display: flex;
    align-items: flex-end;
    gap: 9px;
    padding-top: 11px;
    border-top: 1px dotted var(--line-2);
    white-space: nowrap;
    min-width: 0;
    min-height: 30px;
  }
  .fleft {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 5px;
    align-items: flex-start;
  }
  .frow {
    display: flex;
    gap: 8px;
    align-items: center;
    min-width: 0;
    align-self: stretch;
  }
  .caps {
    font-size: 0.6875rem;
    font-weight: calc(600 + (var(--ui-weight) - 400));
    color: var(--text-3);
    letter-spacing: 0.02em;
  }
  .cbox {
    width: 16px;
    height: 16px;
    border-radius: 4px;
    flex: none;
    display: grid;
    place-items: center;
    border: 1.5px solid var(--line-2);
    background: var(--panel);
    color: var(--panel);
  }
  /* check inherits color:var(--panel) — white-on-accent in light, dark-on-sage in dark */
  .cbox.on {
    background: var(--accent);
    border-color: var(--accent);
  }
  .mcfootid {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    font-size: 0.6875rem;
    color: var(--text-3);
  }
  /* quiet added-by-you marker — a word, not a badge */
  .added {
    flex: none;
    font-size: 0.625rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text-3);
    font-weight: calc(600 + (var(--ui-weight) - 400));
  }
  /* remove: hover/focus-revealed quiet text — discoverable, never loud */
  .rmbtn {
    flex: none;
    border: 0;
    background: none;
    padding: 2px 4px;
    font-family: var(--sans);
    font-size: 0.6875rem;
    font-weight: calc(600 + (var(--ui-weight) - 400));
    color: var(--text-3);
    cursor: pointer;
    opacity: 0;
    transition: opacity 0.13s ease, color 0.13s ease;
  }
  .mcard:hover .rmbtn,
  .rmbtn:focus-visible {
    opacity: 1;
  }
  .rmbtn:hover {
    color: var(--crit, #c0392b);
  }
  .rmbtn:disabled {
    opacity: 1;
    cursor: default;
    color: var(--text-3);
  }
  .tasklist {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 1px;
    flex: none;
    margin-left: auto;
    font-size: 0.6875rem;
    font-weight: calc(600 + (var(--ui-weight) - 400));
    color: var(--text-2);
    line-height: 1.5;
    text-align: right;
  }
  .tasklist .tl {
    font-size: 0.5625rem;
    letter-spacing: 0.11em;
    text-transform: uppercase;
    color: var(--text-3);
  }
  .notasks {
    flex: none;
    font-size: 0.6875rem;
    font-weight: calc(600 + (var(--ui-weight) - 400));
    color: var(--text-3);
  }
  .mcfoot .btn {
    flex: none;
  }
</style>
