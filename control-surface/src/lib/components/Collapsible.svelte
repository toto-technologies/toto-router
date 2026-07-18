<script>
  // Mockup: .card.collapsible — header is the click target, caret rotates, count stays visible.
  let { title = '', meta = '', collapsed = $bindable(false), children } = $props();
  function toggle() { collapsed = !collapsed; }
  function key(e) { if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); toggle(); } }
</script>

<div class="card collapsible" class:collapsed>
  <div
    class="ch"
    role="button"
    tabindex="0"
    aria-expanded={!collapsed}
    onclick={toggle}
    onkeydown={key}
  >
    <h3>{title}</h3>
    {#if meta}<span class="meta">{meta}</span>{/if}
    <span class="caret">
      <svg viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></svg>
    </span>
  </div>
  <div class="cb">{@render children?.()}</div>
</div>
