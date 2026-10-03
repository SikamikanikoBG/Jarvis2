import { useEffect, useMemo, useRef, useState } from 'react';
import { frameDocument } from './frame';

/** One message's HTML body as Outlook renders it, in a sandboxed frame as tall as its content. */
export function MailHtml({ html, title }: { html: string; title: string }) {
  const frame = useRef<HTMLIFrameElement>(null);
  const [height, setHeight] = useState(120);
  const doc = useMemo(() => frameDocument(html), [html]);

  useEffect(() => {
    const el = frame.current;
    if (!el) return;
    let observer: ResizeObserver | null = null;
    const measure = () => {
      const d = el.contentDocument;
      if (!d?.documentElement) return;
      setHeight(Math.max(40, d.documentElement.scrollHeight));
    };
    const onLoad = () => {
      measure();
      observer?.disconnect();
      const body = el.contentDocument?.body;
      if (body && typeof ResizeObserver !== 'undefined') {
        // Images arrive after load and grow the page.
        observer = new ResizeObserver(measure);
        observer.observe(body);
      }
    };
    el.addEventListener('load', onLoad);
    return () => {
      el.removeEventListener('load', onLoad);
      observer?.disconnect();
    };
  }, [doc]);

  return (
    <iframe
      ref={frame}
      className="mail-html"
      title={title}
      srcDoc={doc}
      // Same origin only so the height can be read; without allow-scripts nothing in it runs.
      sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"
      referrerPolicy="no-referrer"
      style={{ height }}
    />
  );
}
