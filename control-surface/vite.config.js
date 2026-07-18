import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

// Dev proxy mirrors frontend/: same-origin /v1 → local gateway (UI-3 wires the client).
// changeOrigin:false so the gateway's Origin==Host same-origin check passes in dev.
const apiTarget = process.env.TOTO_API_PROXY || 'http://localhost:8892';

export default defineConfig({
  plugins: [sveltekit()],
  // Pin the edition to a literal (`__EDITION__`) so edition checks are statically foldable —
  // enterprise-only nav/widgets are dead-code-eliminated from OSS bundles. A bare define
  // (not import.meta.env) so `node --test` can import $lib modules directly (typeof-guarded).
  define: {
    __EDITION__: JSON.stringify(process.env.VITE_EDITION || 'enterprise')
  },
  server: {
    port: 5180,
    proxy: {
      '/v1': { target: apiTarget, changeOrigin: false }
    }
  }
});
