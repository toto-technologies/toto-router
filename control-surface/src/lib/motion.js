// UI-2 · brisk page/reveal motion. Svelte built-ins only (Ponytail: no libs).
// prefers-reduced-motion is a hard gate here — reduced users get duration 0
// (instant, no movement). CSS transitions are gated separately by the global
// @media (prefers-reduced-motion) rule in app.css.
import { fly, fade } from 'svelte/transition';
import { cubicOut } from 'svelte/easing';

const reduced = () =>
  typeof matchMedia !== 'undefined' &&
  matchMedia('(prefers-reduced-motion: reduce)').matches;

// Page navigation: brisk fade + small rise. ~150ms, static under reduced-motion.
export const pageIn = (node) =>
  fly(node, { y: reduced() ? 0 : 8, duration: reduced() ? 0 : 150, easing: cubicOut });

// Skeleton → content reveal after data resolves. Plain cross-fade.
export const revealIn = (node) =>
  fade(node, { duration: reduced() ? 0 : 180 });
