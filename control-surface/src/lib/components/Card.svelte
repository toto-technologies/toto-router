<script>
  // Mockup: .card > (.ch header with h3 + .meta) + .cb body. All styling from app.css.
  let { title = '', meta = '', head, children, class: klass = '' } = $props();
</script>

<div class="card {klass}">
  {#if title || head || meta}
    <div class="ch">
      {#if head}{@render head()}{:else}<h3>{title}</h3>{/if}
      {#if meta}<span class="meta">{meta}</span>{/if}
    </div>
  {/if}
  <div class="cb">{@render children?.()}</div>
</div>
