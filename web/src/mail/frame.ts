/**
 * Inside the frame: no script runs (the CSP and the sandbox both say so), links open in a new
 * tab, and a mail written for a white page gets one - as in Outlook.
 */
const HEAD = [
  '<meta charset="utf-8">',
  `<meta http-equiv="Content-Security-Policy" content="script-src 'none'; object-src 'none'; frame-src 'none'; form-action 'none'">`,
  '<base target="_blank">',
  '<style>',
  'html{background:#fff;color:#1f1f1f;color-scheme:light}',
  'body{margin:0;padding:12px 14px;font:14px/1.45 "Segoe UI",Aptos,Calibri,Arial,sans-serif;overflow-wrap:anywhere}',
  'img{max-width:100%;height:auto}',
  'table{max-width:100%}',
  'pre{white-space:pre-wrap}',
  '</style>',
].join('');

/** A mail's HTML as the frame's document, with the head above put first. */
export function frameDocument(markup: string): string {
  if (/<head[^>]*>/i.test(markup)) return markup.replace(/<head[^>]*>/i, (m) => `${m}${HEAD}`);
  if (/<html[^>]*>/i.test(markup)) return markup.replace(/<html[^>]*>/i, (m) => `${m}<head>${HEAD}</head>`);
  return `<!doctype html><html><head>${HEAD}</head><body>${markup}</body></html>`;
}
