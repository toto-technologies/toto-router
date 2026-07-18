// Safe markdown for Toto's answers — escape-FIRST, then rebuild structure. Model text is an
// untrusted input: every character is HTML-escaped before any tag we emit, so the only markup
// in the output is markup this file wrote. No raw-HTML passthrough, ever (XSS boundary).
//
// Supported (the chat subset): paragraphs, # headings, **bold**, *italic*/_italic_,
// `inline code`, [links](https://…) (http/https/mailto only), - / 1. lists, > blockquotes,
// and ``` fenced code blocks (rendered with a copy affordance the page wires up).
// Pure string -> string so `node --test` covers it without a DOM.

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

/** Escape ALL HTML-significant characters. Runs before any structural pass. */
export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ESC[c]);
}

/** A link target the renderer will emit: http(s) or mailto, nothing else (no javascript:,
 *  data:, vbscript:, protocol-relative, or anything smuggled behind whitespace/controls). */
function safeHref(url) {
  const clean = url.replace(/[\s\u0000-\u0020]+/g, '');
  return /^(https?:\/\/|mailto:)/i.test(clean) ? clean : null;
}

/** Inline spans over ALREADY-ESCAPED text: code first (its content is opaque — no nesting),
 *  then links, bold, italic. */
function inline(text) {
  let out = '';
  // Split on inline-code spans so emphasis/link syntax inside backticks stays literal.
  const parts = text.split(/(`[^`]+`)/);
  for (const part of parts) {
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      out += `<code>${part.slice(1, -1)}</code>`;
      continue;
    }
    let s = part;
    // [text](url) — url is escaped text; validate protocol, emit with rel+target hardening.
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, label, url) => {
      const href = safeHref(url);
      return href
        ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`
        : m;
    });
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/\*([^*\s][^*]*)\*/g, '<em>$1</em>');
    s = s.replace(/\b_([^_]+)_\b/g, '<em>$1</em>');
    out += s;
  }
  return out;
}

/** Escaped, structured HTML for one markdown string. Code-block copy buttons carry
 *  class="md-copy" — the page delegates one click handler to them. */
export function renderMarkdown(src) {
  if (!src) return '';
  const lines = String(src).replace(/\r\n/g, '\n').split('\n');
  const html = [];
  let para = []; // pending paragraph lines
  let list = null; // {tag:'ul'|'ol', items:[]}

  const flushPara = () => {
    if (para.length) {
      html.push(`<p>${inline(escapeHtml(para.join('\n')).replace(/\n/g, '<br>'))}</p>`);
      para = [];
    }
  };
  const flushList = () => {
    if (list) {
      html.push(`<${list.tag}>${list.items.map((i) => `<li>${i}</li>`).join('')}</${list.tag}>`);
      list = null;
    }
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // Fenced code block: swallow lines to the closing fence (or EOF). Content is escaped
    // whole — nothing inside a fence is ever parsed as markdown or HTML.
    const fence = line.match(/^```([\w+-]*)\s*$/);
    if (fence) {
      flushPara();
      flushList();
      const buf = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) buf.push(lines[i++]);
      const lang = escapeHtml(fence[1] || '');
      html.push(
        `<div class="md-code">` +
          `<div class="md-code-bar"><span>${lang || 'code'}</span>` +
          `<button type="button" class="md-copy" aria-label="Copy code">Copy</button></div>` +
          `<pre><code>${escapeHtml(buf.join('\n'))}</code></pre></div>`
      );
      continue;
    }

    if (!line.trim()) {
      flushPara();
      flushList();
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      flushPara();
      flushList();
      const level = heading[1].length + 2; // #→h3 … inside a chat message, never page-level h1/h2
      html.push(`<h${level}>${inline(escapeHtml(heading[2]))}</h${level}>`);
      continue;
    }

    const quote = line.match(/^>\s?(.*)$/);
    if (quote) {
      flushPara();
      flushList();
      html.push(`<blockquote>${inline(escapeHtml(quote[1]))}</blockquote>`);
      continue;
    }

    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ul || ol) {
      flushPara();
      const tag = ul ? 'ul' : 'ol';
      if (!list || list.tag !== tag) {
        flushList();
        list = { tag, items: [] };
      }
      list.items.push(inline(escapeHtml((ul || ol)[1])));
      continue;
    }

    flushList();
    para.push(line);
  }
  flushPara();
  flushList();
  return html.join('');
}
