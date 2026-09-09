/**
 * Which build of the SPA is running.
 *
 * The version in the header is the CORE's, from `/api/health` — so shipping web-only changes
 * left the running app showing the same number release after release, with no way to tell from
 * inside it whether the new UI had actually landed. This is the other half of that answer.
 */
export const WEB_VERSION: string = typeof __WEB_VERSION__ === 'string' ? __WEB_VERSION__ : 'dev';

/** "2.0.0-alpha.11" → "alpha.11": what changes between web releases, without the shared prefix. */
export function shortWebVersion(full: string = WEB_VERSION): string {
  const dash = full.indexOf('-');
  return dash > 0 ? full.slice(dash + 1) : full;
}
