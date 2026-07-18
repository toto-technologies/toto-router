<script>
  // Mockup: .scrim > .modal (.mh header / .mb body / .mf footer). bindable `open`.
  // `danger` tints the header like the kill-switch confirm. Esc + scrim-click close.
  let { open = $bindable(false), title = '', subtitle = '', danger = false, header, children, footer } = $props();
  function close() { open = false; }
  function key(e) { if (e.key === 'Escape') close(); }
</script>

<svelte:window onkeydown={open ? key : undefined} />

<div class="scrim" class:open role="presentation" onclick={close}>
  {#if open}
    <div class="modal" role="dialog" aria-modal="true" aria-label={title} onclick={(e) => e.stopPropagation()}>
      {#if header}
        {@render header()}
      {:else if title}
        <div class="mh" style={danger ? '' : 'background:none'}>
          <h3>{title}</h3>
          {#if subtitle}<p>{subtitle}</p>{/if}
        </div>
      {/if}
      <div class="mb">{@render children?.()}</div>
      {#if footer}<div class="mf">{@render footer()}</div>{/if}
    </div>
  {/if}
</div>
