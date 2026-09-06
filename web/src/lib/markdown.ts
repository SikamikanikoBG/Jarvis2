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
  });
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
