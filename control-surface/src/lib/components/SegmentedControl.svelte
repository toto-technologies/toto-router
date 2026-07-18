<script>
  // Mockup: .seg (optional .cyan for accent-on). options = [{value,label}] or strings. bindable `value`.
  // disabled: shows the current selection but ignores clicks (org-wide value, no writable API yet).
  let { options = [], value = $bindable(), accent = true, onchange, disabled = false } = $props();
  const norm = (o) => (typeof o === 'string' ? { value: o, label: o } : o);
  function pick(v) { if (disabled) return; value = v; onchange?.(v); }
</script>

<div class="seg" class:cyan={accent} aria-disabled={disabled} style={disabled ? 'opacity:.7' : ''}>
  {#each options.map(norm) as opt}
    <button class:on={value === opt.value} disabled={disabled && value !== opt.value} style={disabled ? 'cursor:default' : ''} onclick={() => pick(opt.value)}>{opt.label}</button>
  {/each}
</div>
