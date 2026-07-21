<script>
  // Self-contained "Add local model" flow — mountable from any page (Catalog today, Settings
  // planned). Contract: `bind:open` to show it; `onadded(entry)` fires after a successful POST
  // so the host can reload whatever catalog views it renders. No other host state touched.
  import Modal from '$lib/components/Modal.svelte';
  import { createLocalModel } from '$lib/api/admin.js';

  let { open = $bindable(false), onadded = () => {} } = $props();

  const PRESETS = [
    ['Ollama', 'http://localhost:11434/v1'],
    ['LM Studio', 'http://localhost:1234/v1'],
    ['vLLM', 'http://localhost:8000/v1'],
  ];
  let name = $state('');
  let url = $state('');
  let model = $state('');
  let err = $state(null);
  let saving = $state(false);

  $effect(() => {
    // Fresh form each time the host opens the modal.
    if (open) {
      name = '';
      url = '';
      model = '';
      err = null;
    }
  });

  async function submit() {
    saving = true;
    err = null;
    try {
      const r = await createLocalModel({ name: name.trim(), baseUrl: url.trim(), model: model.trim() });
      open = false;
      onadded(r?.entry);
    } catch (e) {
      err = e?.message ?? 'Could not add the local model';
    } finally {
      saving = false;
    }
  }
</script>

<Modal
  bind:open
  title="Add local model"
  subtitle="Point Toto at an OpenAI-compatible server running on your machine or network. No API key needed."
>
  <div class="field">
    <label for="lm-url">Server URL</label>
    <input id="lm-url" bind:value={url} placeholder="http://localhost:11434/v1" spellcheck="false" />
    <div class="presets">
      {#each PRESETS as [label, preset] (preset)}
        <button class="btn small ghost" class:active={url === preset} onclick={() => (url = preset)}>{label}</button>
      {/each}
    </div>
  </div>
  <div class="field">
    <label for="lm-model">Model name</label>
    <input id="lm-model" bind:value={model} placeholder="llama3.1" spellcheck="false" />
    <div class="fieldnote">The model name exactly as your server knows it (e.g. `ollama list`).</div>
  </div>
  <div class="field">
    <label for="lm-name">Display name (optional)</label>
    <input id="lm-name" bind:value={name} placeholder="Llama on Ollama" />
  </div>
  {#if err}<div class="fielderr">{err}</div>{/if}
  {#snippet footer()}
    <button class="btn ghost" onclick={() => (open = false)}>Cancel</button>
    <button class="btn primary" disabled={saving || !url.trim() || !model.trim()} onclick={submit}>
      {saving ? 'Adding…' : 'Add local model'}
    </button>
  {/snippet}
</Modal>

<style>
  .presets {
    display: flex;
    gap: 6px;
    margin-top: 8px;
  }
  .presets .btn.active {
    border-color: var(--accent-line);
    color: var(--accent);
  }
  .fieldnote {
    margin-top: 6px;
    font-size: 0.65625rem;
    color: var(--text-3);
  }
  .fielderr {
    margin-top: 6px;
    font-size: 0.71875rem;
    color: var(--crit);
  }
  .btn.ghost {
    background: transparent;
  }
</style>
