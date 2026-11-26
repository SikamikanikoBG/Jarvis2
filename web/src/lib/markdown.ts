import DOMPurify from 'dompurify';
import { marked } from 'marked';

marked.use({
  gfm: true,
  breaks: true,
  async: false,
  renderer: {
    // Every fenced block gets a header with its language and a copy control (a span, not a
    // button: DOMPurify strips buttons; Markdown.tsx handles the click by delegation).
    code({ text, lang }: { text: string; lang?: string }): string {
      const language = (lang ?? '').trim().split(/\s+/)[0] ?? '';
      const cls = language ? ` class="language-${escapeHtml(language)}"` : '';
      return (
        `<div class="codeblock"><div class="codehead"><span class="codelang">${escapeHtml(language || 'text')}</span>` +
        `<span class="code-copy" role="button" tabindex="0" title="Copy code">Copy</span></div>` +
        `<pre><code${cls}>${escapeHtml(text)}\n</code></pre></div>`
      );
    },
  },
});

let hooked = false;
function ensureHooks(): void {
  if (hooked) return;
  hooked = true;
  DOMPurify.addHook('afterSanitizeAttributes', (node) => {
    if (node.tagName === 'A') {
      node.setAttribute('target', '_blank');
      node.setAttribute('rel', 'noopener noreferrer');
    }
    if (node.tagName === 'TD' || node.tagName === 'TH') {
      const cls = cellWidthClass(node.textContent?.length ?? 0);
      if (cls) node.classList.add(cls);
    }
  });
}

/**
 * How wide a table cell may not get narrower than, by how much text it holds. A table squeezed
 * into the bubble wrapped its long cells into narrow columns hundreds of pixels tall (2026-11-26,
 * a phone on its side); with a floor that grows with the text, a short column stays short, a long
 * one gets room to read, and a table wider than the bubble scrolls sideways.
 */
export function cellWidthClass(chars: number): string | null {
  if (chars <= 12) return null;
  if (chars <= 40) return 'cell-w1';
  if (chars <= 120) return 'cell-w2';
  if (chars <= 300) return 'cell-w3';
  return 'cell-w4';
}

const cache = new Map<string, string>();

/** Markdown → sanitised HTML. Memoised on the source text (streaming re-renders the same prefix). */
export function renderMarkdown(src: string): string {
  ensureHooks();
  const hit = cache.get(src);
  if (hit !== undefined) return hit;
  const raw = marked.parse(src) as string;
  const html = DOMPurify.sanitize(raw, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ['style', 'iframe', 'form', 'input', 'button'],
    FORBID_ATTR: ['style', 'onerror', 'onload'],
  });
  if (cache.size > 400) cache.clear();
  cache.set(src, html);
  return html;
}

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] ?? c);
}
