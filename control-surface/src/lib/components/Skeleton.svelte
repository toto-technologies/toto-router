<script>
  // Loading placeholder. Not in the mockup CSS — minimal, token-driven, theme-aware.
  let { width = '100%', height = '14px', radius = '6px' } = $props();
</script>

<span class="sk" style="width:{width};height:{height};border-radius:{radius}"></span>

<style>
  .sk {
    display: block;
    background: linear-gradient(90deg, var(--panel-2) 25%, var(--panel-hi) 50%, var(--panel-2) 75%);
    background-size: 200% 100%;
    animation: sk 1.15s ease-in-out infinite;
  }
  @keyframes sk { to { background-position: -200% 0; } }
  @media (prefers-reduced-motion: reduce) { .sk { animation: none; } }
</style>
