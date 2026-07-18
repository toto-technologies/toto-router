// The ONLY widget registry. Array order = default layout order (brief §3.2: money first,
// traffic + what's serving it, health + savings, evidence + people, then the audit trail).
// The layout store appends any id missing from stored state in THIS order.
import SpendKpis from './widgets/SpendKpis.svelte';
import RequestVolume from './widgets/RequestVolume.svelte';
import Models from './widgets/Models.svelte';
import ProviderHealth from './widgets/ProviderHealth.svelte';
import CacheSavings from './widgets/CacheSavings.svelte';
import TeamsPeople from './widgets/TeamsPeople.svelte';
import Benchmarks from './widgets/Benchmarks.svelte';
import RecentActivity from './widgets/RecentActivity.svelte';

/** Humanized page-range label — shared by the range-aware widget headers + page status line. */
export const rangeLabel = (r) =>
  ({ '24h': 'last 24 hours', '7d': 'last 7 days', '30d': 'last 30 days' })[r] ?? r;

// title = the human name used in the hidden tray, aria announcements, and edit chrome.
// Widgets set their own display heading (e.g. "How you're doing") on their WidgetFrame.
const CORE_WIDGETS = [
  { id: 'spend', title: 'Spend & usage', href: '/usage', linkLabel: 'See usage & billing', sizes: ['sm', 'lg'], defaultSize: 'lg', component: SpendKpis },
  { id: 'volume', title: 'Request volume', href: '/activity', linkLabel: 'See all activity', sizes: ['sm', 'lg'], defaultSize: 'sm', component: RequestVolume },
  { id: 'models', title: 'Your top models', href: '/catalog', linkLabel: 'Manage catalog & routing', sizes: ['sm', 'lg'], defaultSize: 'sm', component: Models }
];

// Edition seam ($lib/edition.js): OSS keeps only widgets whose targets exist in the OSS
// console; __EDITION__ is a build-time literal (inlined here so it folds within this
// module), so the enterprise rows AND their components are dead-code-eliminated from OSS
// bundles. Stored layouts with enterprise ids degrade gracefully — the loader drops unknown ids.
const OSS = typeof __EDITION__ !== 'undefined' && __EDITION__ === 'oss';
export const WIDGETS = OSS
  ? CORE_WIDGETS
  : [
      ...CORE_WIDGETS,
      { id: 'health', title: 'Provider health', href: '/analytics', linkLabel: 'See routing status', sizes: ['sm', 'lg'], defaultSize: 'sm', component: ProviderHealth },
      { id: 'cache', title: 'Cache savings', href: '/cache', linkLabel: 'See cache details', sizes: ['sm'], defaultSize: 'sm', component: CacheSavings },
      { id: 'benchmarks', title: 'Benchmark standouts', href: '/benchmarks', linkLabel: 'See all benchmarks', sizes: ['sm', 'lg'], defaultSize: 'sm', component: Benchmarks },
      { id: 'teams', title: 'Teams & people', href: '/teams', linkLabel: 'Manage teams & people', sizes: ['sm'], defaultSize: 'sm', component: TeamsPeople },
      { id: 'activity', title: 'Recent activity', href: '/audit', linkLabel: 'See full audit log', sizes: ['sm', 'lg'], defaultSize: 'lg', component: RecentActivity }
    ];
