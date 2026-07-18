<script>
  // Mockup: .tablewrap > table > thead/tbody. Pass `head` (a <tr> of <th>) and
  // `children` (the <tr> rows) snippets. `wrap=false` drops the scroll wrapper
  // (e.g. inside a ScrollList / collapsible card).
  let { head, children, wrap = true } = $props();
</script>

{#snippet table()}
  <table>
    {#if head}<thead>{@render head()}</thead>{/if}
    <tbody>{@render children?.()}</tbody>
  </table>
{/snippet}

{#if wrap}
  <div class="tablewrap">{@render table()}</div>
{:else}
  {@render table()}
{/if}
