// The Control Surface categories, grouped like the a-forest mockup rail.
// icon = raw <path>/<circle> inner SVG (24x24 viewBox), lifted from the mockup.
//
// Edition seam: the OSS tabs are shared consts; __EDITION__ is a build-time literal
// (see vite.config.js + $lib/edition.js), so the unused edition's group list below is dead
// code the bundler drops — the OSS bundle carries no enterprise nav entries at all. The
// check is inlined (not imported from edition.js) so it folds within this module.
const OSS = typeof __EDITION__ !== 'undefined' && __EDITION__ === 'oss';
const OVERVIEW = { href: '/overview', label: 'Overview', icon: '<path d="M4 13h6V4H4zM14 20h6V4h-6zM4 20h6v-4H4z"/>' };
const ACTIVITY = { href: '/activity', label: 'Activity', icon: '<path d="M3 12h4l2 6 4-14 2 8h6"/>' };
const MODELS = { href: '/models', label: 'Models', icon: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>' };
const CATALOG = { href: '/catalog', label: 'Catalog & Routing', icon: '<path d="M4 6h16M4 12h16M4 18h10"/><circle cx="18" cy="18" r="2.4"/>' };
const CACHING = { href: '/caching', label: 'Caching', icon: '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/>' };
const USAGE = { href: '/usage', label: 'Usage & Billing', icon: '<path d="M4 19V5M4 19h16M8 16l3-4 3 2 4-6"/>' };
const SETTINGS = { href: '/settings', label: 'Settings', icon: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>' };

export const NAV_GROUPS = OSS
  ? [
      { label: 'Monitor', items: [OVERVIEW, ACTIVITY] },
      { label: 'Policy', items: [MODELS, CATALOG, CACHING] },
      { label: 'Telemetry', items: [USAGE] },
      { label: 'System', items: [SETTINGS] }
    ]
  : [
      {
        // Chat-forward: the companion is the product's center of gravity, so it leads the rail.
        // The locked v18 groups below are untouched.
        label: 'Companion',
        items: [
          { href: '/chat', label: 'Chat', icon: '<path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5H7.4L3 23V11.5A8.5 8.5 0 0 1 11.5 3h1A8.5 8.5 0 0 1 21 11.5z"/><path d="M8.5 10.5h7M8.5 13.5h4.5"/>' },
          { href: '/documents', label: 'Documents', icon: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/>' }
        ]
      },
      {
        label: 'Monitor',
        items: [
          OVERVIEW,
          ACTIVITY,
          { href: '/labeling', label: 'Routing review', icon: '<circle cx="12" cy="12" r="9"/><path d="M8.5 12.5l2.5 2.5 4.5-5"/>' }
        ]
      },
      {
        label: 'Policy',
        items: [
          { href: '/teams', label: 'Teams & People', icon: '<circle cx="9" cy="8" r="3"/><path d="M3 20a6 6 0 0 1 12 0M16 6a3 3 0 0 1 0 6M21 20a5 5 0 0 0-4-5"/>' },
          MODELS,
          CATALOG,
          { href: '/pricing', label: 'Pricing', icon: '<path d="M20.6 13.4L11 3.8A2 2 0 0 0 9.6 3H5a2 2 0 0 0-2 2v4.6c0 .53.21 1.04.59 1.41l9.6 9.6a2 2 0 0 0 2.82 0l4.6-4.6a2 2 0 0 0-.01-2.61z"/><circle cx="7.5" cy="7.5" r="1.5"/>' },
          { href: '/cache', label: 'Cache', icon: '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/>' },
          { href: '/benchmarks', label: 'Benchmarks', icon: '<path d="M5 20V10M12 20V4M19 20v-7"/>' },
          { href: '/tuning', label: 'Tuning', icon: '<path d="M6 4v6M6 14v6M12 4v10M12 18v2M18 4v2M18 10v10"/><circle cx="6" cy="12" r="2"/><circle cx="12" cy="16" r="2"/><circle cx="18" cy="8" r="2"/>' },
          { href: '/budgets', label: 'Budgets & Limits', icon: '<path d="M12 3v18M8 7h6a3 3 0 0 1 0 6H8m0 0h7"/>' },
          { href: '/governance', label: 'Governance', icon: '<path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"/>' }
        ]
      },
      {
        label: 'Telemetry',
        items: [
          { href: '/analytics', label: 'Analytics', icon: '<circle cx="12" cy="12" r="9"/><path d="M12 3v9h9M12 12l-6.4 6.4"/>' },
          USAGE,
          { href: '/org-insights', label: 'Org Insights', icon: '<circle cx="12" cy="12" r="3"/><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/>' },
          { href: '/audit', label: 'Audit', icon: '<path d="M9 6h9M9 12h9M9 18h5M4 6h.01M4 12h.01M4 18h.01"/>' }
        ]
      },
      {
        label: 'System',
        items: [SETTINGS]
      }
    ];
