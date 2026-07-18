import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
  preprocess: vitePreprocess(),
  kit: {
    // Standalone static console — every route prerenders (see routes/+layout.js).
    // fallback lets client-side nav own any path the crawler misses.
    adapter: adapter({ fallback: '200.html' }),
    // OSS build (npm run build:oss) compiles from a pruned routes dir — enterprise pages are
    // excluded at build time, not hidden at runtime (dev/make-oss-routes.mjs generates it).
    files: { routes: process.env.CONSOLE_ROUTES || 'src/routes' },
    // Same-origin mount under the gateway. Prod build sets CONSOLE_BASE=/console (see Dockerfile);
    // dev (`npm run dev`, no env) keeps base '' so http://localhost:5180/ works unchanged.
    paths: { base: process.env.CONSOLE_BASE || '', relative: false }
  }
};

export default config;
