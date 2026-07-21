// Shared model-name prettifier + provider label — ONE source so the Catalog and Benchmarks pages
// render identical, clean names. Combines the best of both prior copies:
//  • strip the provider path prefix   (anthropic/claude-sonnet-5, accounts/fireworks/models/glm-5p2
//                                       → the last segment)
//  • drop a leading routing alias      (or-/fw-) and a trailing date stamp (…-20260101 / :20260101)
//  • split on -/_ (version dots survive), keep version tokens (4o, 3.5, 70b, v2),
//    upper-case known vendor words (GPT/AI/LLM/GLM), title-case the rest.
// ponytail: a dash-version benchmark alias like "claude-sonnet-4-6" still reads "4 6" — that's a
// benchmarks.yaml data artifact (a dashed duplicate key), not something to special-case here.
const VENDOR_WORD = { gpt: 'GPT', ai: 'AI', llm: 'LLM', glm: 'GLM' };

export function prettyModel(raw) {
  const s0 = (raw && (raw.upstream_model || raw.id)) || (typeof raw === 'string' ? raw : '') || '';
  const s = s0.split('#')[0]; // drop the deployment pin (…/models/x#…/deployments/y)
  const base = s.includes('/') ? s.slice(s.lastIndexOf('/') + 1) : s;
  const cleaned = base.replace(/^(or|fw)-/, '').replace(/[:@-]\d{6,8}$/, '');
  return (
    cleaned
      .split(/[-_]/)
      .filter(Boolean)
      .map((w) => {
        const lw = w.toLowerCase();
        if (VENDOR_WORD[lw]) return VENDOR_WORD[lw];
        if (/^\d/.test(w)) return w; // keep tokens that START with a digit (4o, 3.5, 70b) (4o, 3.5, 70b, v2)
        return w.charAt(0).toUpperCase() + w.slice(1);
      })
      .join(' ') || s
  );
}

export function providerLabel(p) {
  const map = { anthropic: 'Anthropic', openai: 'OpenAI', openrouter: 'OpenRouter',
                fireworks: 'Fireworks', cloudflare: 'Cloudflare', local: 'Local', fake: 'Fake',
                google: 'Google' };
  if (!p) return '';
  return map[p] || (p.charAt(0).toUpperCase() + p.slice(1));
}

// Adaptive $/1k price formatter — 2 decimals for >=$1 (3.00, 15.00); sub-dollar keeps enough
// significant figures so a real sub-cent rate isn't rounded away to "0.00" (0.0003, 0.0025).
// Shared by the Catalog + Benchmarks price columns so they can't drift.
export function priceFmt(n) {
  if (n == null) return '—';
  if (n === 0) return '0';
  if (n >= 1) return n.toFixed(2);
  return parseFloat(n.toPrecision(2)).toString();
}
