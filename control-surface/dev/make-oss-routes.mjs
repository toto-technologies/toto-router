// Generates src/routes-oss/ — the OSS console's pruned route tree (gitignored).
// SvelteKit has no route-filter hook, so `build:oss` materializes a copy of src/routes
// holding only the six OSS tabs and points kit.files.routes at it via CONSOLE_ROUTES.
// Enterprise pages are never compiled into the OSS bundle.
import { cpSync, rmSync, readdirSync } from 'node:fs';

const SRC = new URL('../src/routes/', import.meta.url);
const DST = new URL('../src/routes-oss/', import.meta.url);
const KEEP = new Set([
  '+layout.js', '+layout.svelte', '+page.js', // shell + root redirect to /overview
  'overview', 'activity', 'models', 'catalog', 'usage', 'settings'
]);

rmSync(DST, { recursive: true, force: true });
const kept = readdirSync(SRC).filter((name) => KEEP.has(name));
for (const name of kept) cpSync(new URL(name, SRC), new URL(name, DST), { recursive: true });
console.log(`routes-oss: kept ${kept.join(', ')}`);
