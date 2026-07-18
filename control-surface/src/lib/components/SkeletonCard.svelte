<script>
  // Card-shaped loading placeholder (title + N body lines). Drop-in for any
  // Card body while it loads; cross-fade to real content with motion.revealIn.
  import Skeleton from './Skeleton.svelte';
  let { lines = 3, title = true } = $props();
</script>

<div class="card">
  <div class="cb sk-stack">
    {#if title}<Skeleton width="42%" height="12px" />{/if}
    {#each Array(lines) as _, i}
      <Skeleton width={i === lines - 1 ? '70%' : '100%'} height="11px" />
    {/each}
  </div>
</div>

<style>
  /* only spacing — colors/animation live in Skeleton.svelte + tokens */
  .sk-stack { display: flex; flex-direction: column; gap: 10px; }
</style>
