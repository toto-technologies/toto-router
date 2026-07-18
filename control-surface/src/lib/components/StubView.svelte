<script>
  // Wave-1 route stub: renders under the shell with its page title + a placeholder card.
  // Wave 2 replaces the body with the real page.
  let { title, sub = '', blurb = '' } = $props();
</script>

<svelte:head><title>{title} · Toto Control</title></svelte:head>

<div class="pagehead">
  <div>
    <h1>{title}</h1>
    {#if sub}<div class="sub">{sub}</div>{/if}
  </div>
</div>

<div class="stub">
  <div class="ic">
    <svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h10" /><circle cx="18" cy="18" r="2.4" /></svg>
  </div>
  <b>{title}</b>
  <p>{blurb || 'This surface is scaffolded. Wave 2 wires it to the admin API.'}</p>
</div>
