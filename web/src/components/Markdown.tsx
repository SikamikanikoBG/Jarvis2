import { useMemo, type KeyboardEvent, type MouseEvent } from 'react';
import { copyText } from '../lib/export';
import { renderMarkdown } from '../lib/markdown';

interface Props {
  text: string;
  className?: string;
}

/** Sanitised markdown. Memoised per text so a streaming re-render only re-parses the new text. */
export function Markdown({ text, className }: Props) {
  const html = useMemo(() => renderMarkdown(text), [text]);
  return (
    <div
      className={className ? `md ${className}` : 'md'}
      dangerouslySetInnerHTML={{ __html: html }}
      onClick={(e) => void handleCodeCopy(e)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') void handleCodeCopy(e);
      }}
    />
  );
}

/** Delegated handler for the copy control the renderer puts on every code block. */
async function handleCodeCopy(e: MouseEvent<HTMLDivElement> | KeyboardEvent<HTMLDivElement>): Promise<void> {
  const target = e.target as HTMLElement | null;
  const control = target?.closest?.('.code-copy') as HTMLElement | null;
  if (!control) return;
  e.preventDefault();
  const code = control.closest('.codeblock')?.querySelector('pre code');
  const ok = await copyText((code?.textContent ?? '').replace(/\n$/, ''));
  const before = control.textContent;
  control.textContent = ok ? 'Copied' : 'Failed';
  setTimeout(() => {
    control.textContent = before;
  }, 1200);
}
