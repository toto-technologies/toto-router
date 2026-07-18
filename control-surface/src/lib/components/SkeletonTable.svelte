<script>
  // Table-shaped loading placeholder. Wave-2 pages render this while data loads,
  // then cross-fade (motion.revealIn) to the real <Table>. Uses the mockup's own
  // .card/.tablewrap/table classes so the skeleton matches the resolved layout.
  import Skeleton from './Skeleton.svelte';
  let { rows = 6, cols = 4, title = true } = $props();
</script>

<div class="card">
  {#if title}
    <div class="ch"><Skeleton width="140px" height="12px" /></div>
  {/if}
  <div class="tablewrap">
    <table>
      <tbody>
        {#each Array(rows) as _, r}
          <tr>
            {#each Array(cols) as _, c}
              <td><Skeleton width={c === 0 ? '62%' : '38%'} height="11px" /></td>
            {/each}
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
</div>
