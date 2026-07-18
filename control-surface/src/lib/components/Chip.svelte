<script>
  // Mockup: .chip pill. variant maps to .perim / .cloud / .ovr accents; `dot` shows the leading dot.
  let { variant = '', dot = false, children } = $props();
</script>

<span class="chip {variant}">
  {#if dot}<span class="d"></span>{/if}
  {@render children?.()}
</span>
