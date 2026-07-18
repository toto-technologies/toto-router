<script>
  // ⚙ Typography-tuning panel — writes CSS vars on :root, persists to localStorage.
  // Markup + logic ported VERBATIM from the a-forest mockup (its own px type never rescales).
  import { onMount } from 'svelte';

  let open = $state(false);
  let panel; // bind:this — scope all lookups to this component's panel

  onMount(() => {
    const root = document.documentElement;
    const KEY = 'toto-tuning';
    const load = () => { try { return JSON.parse(localStorage.getItem(KEY) || '{}'); } catch { return {}; } };
    const save = (s) => { try { localStorage.setItem(KEY, JSON.stringify(s)); } catch {} };
    let state = load();
    const ctrls = [].slice.call(panel.querySelectorAll('[data-var]'));
    const fmt = {
      '--type-scale': (v) => (+v).toFixed(2) + '×',
      '--ui-weight': (v) => '' + Math.round(+v),
      '--ui-leading': (v) => (+v).toFixed(2),
      '--ui-tracking': (v) => (+v).toFixed(3) + 'em'
    };
    const vspan = { '--type-scale': 'v-scale', '--ui-weight': 'v-weight', '--ui-leading': 'v-leading', '--ui-tracking': 'v-track' };
    const byId = (id) => panel.querySelector('#' + id);
    function reflect(el) {
      const id = vspan[el.dataset.var];
      if (id && fmt[el.dataset.var]) byId(id).textContent = fmt[el.dataset.var](el.value);
    }
    function apply(el) {
      const v = el.value + (el.dataset.suffix || '');
      root.style.setProperty(el.dataset.var, v); reflect(el);
      state[el.dataset.var] = v; save(state);
    }
    ctrls.forEach((el) => {
      const saved = state[el.dataset.var];
      if (saved != null) { el.value = el.dataset.suffix ? saved.replace(el.dataset.suffix, '') : saved; }
      reflect(el);
      el.addEventListener(el.tagName === 'SELECT' ? 'change' : 'input', () => apply(el));
    });
    byId('tuneReset').addEventListener('click', () => {
      state = {}; save(state);
      ['--type-scale', '--sans', '--mono', '--ui-weight', '--ui-leading', '--ui-tracking', '--density']
        .forEach((k) => root.style.removeProperty(k));
      ctrls.forEach((el) => {
        if (el.tagName === 'SELECT') {
          let i = 0;
          for (let j = 0; j < el.options.length; j++) { if (el.options[j].defaultSelected) { i = j; break; } }
          el.selectedIndex = i;
        } else { el.value = el.getAttribute('value'); }
        reflect(el);
      });
    });
  });
</script>

<button id="tuneFab" title="Typography tuning" aria-label="Typography tuning" onclick={() => (open = !open)}>⚙</button>

<div id="tunePanel" class:open bind:this={panel} role="dialog" aria-label="Typography tuning">
  <div class="th"><b>Typography</b><button class="reset" id="tuneReset">Reset</button></div>

  <div class="tc"><div class="tl"><label for="t-scale">Font size</label><span class="v" id="v-scale">1.00×</span></div>
    <input type="range" data-var="--type-scale" id="t-scale" min="0.85" max="1.25" step="0.01" value="1" /></div>

  <div class="tc"><div class="tl"><label for="t-sans">UI font</label></div>
    <select data-var="--sans" id="t-sans">
      <option value="Inter,-apple-system,&quot;Helvetica Neue&quot;,Helvetica,Arial,sans-serif" selected>Inter (default)</option>
      <option value="system-ui,-apple-system,&quot;Segoe UI&quot;,Roboto,Helvetica,Arial,sans-serif">System sans</option>
      <option value="&quot;Helvetica Neue&quot;,Helvetica,&quot;Arial Nova&quot;,Arial,&quot;Liberation Sans&quot;,sans-serif">Grotesk</option>
      <option value="&quot;Segoe UI&quot;,&quot;Gill Sans&quot;,&quot;Gill Sans MT&quot;,Calibri,&quot;Trebuchet MS&quot;,sans-serif">Humanist</option>
      <option value="Georgia,Cambria,&quot;Times New Roman&quot;,&quot;Noto Serif&quot;,serif">Serif</option>
      <option value="ui-monospace,&quot;SF Mono&quot;,Menlo,Consolas,monospace">Mono everything</option>
    </select></div>

  <div class="tc"><div class="tl"><label for="t-mono">Data / mono font</label></div>
    <select data-var="--mono" id="t-mono">
      <option value="&quot;Courier New&quot;,Courier,monospace" selected>Courier (default)</option>
      <option value="ui-monospace,&quot;SF Mono&quot;,&quot;JetBrains Mono&quot;,Menlo,&quot;Cascadia Mono&quot;,Consolas,monospace">Mono stack</option>
      <option value="Menlo,Consolas,&quot;Courier New&quot;,monospace">Menlo · Consolas</option>
      <option value="var(--sans)">Match UI</option>
    </select></div>

  <div class="tc"><div class="tl"><label for="t-weight">Boldness</label><span class="v" id="v-weight">425</span></div>
    <input type="range" data-var="--ui-weight" id="t-weight" min="300" max="600" step="25" value="425" /></div>

  <div class="tc"><div class="tl"><label for="t-leading">Line height</label><span class="v" id="v-leading">1.45</span></div>
    <input type="range" data-var="--ui-leading" id="t-leading" min="1.2" max="1.9" step="0.05" value="1.45" /></div>

  <div class="tc"><div class="tl"><label for="t-track">Letter spacing</label><span class="v" id="v-track">-0.005em</span></div>
    <input type="range" data-var="--ui-tracking" data-suffix="em" id="t-track" min="-0.02" max="0.06" step="0.005" value="-0.005" /></div>

  <div class="tc"><div class="tl"><label for="t-density">Density</label></div>
    <select data-var="--density" id="t-density">
      <option value="0.72">Compact</option>
      <option value="1" selected>Cozy</option>
      <option value="1.3">Comfortable</option>
    </select></div>
</div>
