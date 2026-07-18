<script>
  // Mockup: .sw switch (.on when checked). Keyboard: Space/Enter. bindable `checked`.
  // disabled: inert display-only state (e.g. an org-wide value with no writable API yet) —
  // still shows the value, just can't be flipped. No new class; opacity+cursor inline is enough.
  let { checked = $bindable(false), label = 'Toggle', onchange, disabled = false } = $props();
  function flip() { if (disabled) return; checked = !checked; onchange?.(checked); }
  function key(e) {
    if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); flip(); }
  }
</script>

<div
  class="sw"
  class:on={checked}
  role="switch"
  aria-checked={checked}
  aria-disabled={disabled}
  aria-label={label}
  tabindex={disabled ? -1 : 0}
  style={disabled ? 'opacity:.55;cursor:default' : ''}
  onclick={flip}
  onkeydown={key}
></div>
