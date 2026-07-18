<script>
  // Mockup: .scrollist — contained self-scrolling region (no marathon page scroll).
  let { max = '342px', children } = $props();
</script>

<div class="scrollist" style="max-height:{max}">{@render children?.()}</div>
