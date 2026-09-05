import { useMemo } from 'react';
import { renderMarkdown } from '../lib/markdown';

interface Props {
  text: string;
  className?: string;
}

/** Sanitised markdown. Memoised per text so a streaming re-render only re-parses the new text. */
export function Markdown({ text, className }: Props) {
  const html = useMemo(() => renderMarkdown(text), [text]);
  return <div className={className ? `md ${className}` : 'md'} dangerouslySetInnerHTML={{ __html: html }} />;
}
