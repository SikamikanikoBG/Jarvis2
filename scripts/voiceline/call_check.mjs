// A whole call through the app, headless: Chromium's fake microphone plays a WAV (a question, a
// cut-in, a puzzle), the call button is pressed, and the far end's level is sampled from the
// line's own peer connection every 250 ms. The line's log (docker logs jarvis2-voiceline) and
// the conversation say the rest. No loudspeaker reaches a headless microphone: whether a phone
// hears itself is not something this can show.
//
//   docker run --rm --network host -v $PWD:/work -v <dir with question.wav>:/audio -w /work \
//     -e APP_URL='http://127.0.0.1:9021/?token=...' mcr.microsoft.com/playwright:v1.63.0-noble \
//     sh -c 'npm i --no-save --silent playwright@1.63.0 && node call_check.mjs'
import { writeFileSync } from 'node:fs';
import { chromium } from 'playwright';

const URL = process.env.APP_URL;
const SECONDS = Number(process.env.CALL_SECONDS || 90);
const browser = await chromium.launch({
  args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream', '--use-file-for-fake-audio-capture=/audio/question.wav', '--autoplay-policy=no-user-gesture-required'],
});
const page = await browser.newPage();
await page.addInitScript(() => {
  const Native = window.RTCPeerConnection;
  window.__pcs = [];
  window.RTCPeerConnection = function (...a) { const pc = new Native(...a); window.__pcs.push(pc); return pc; };
  window.RTCPeerConnection.prototype = Native.prototype;
});
page.on('console', (m) => m.type() === 'error' && console.log('[page error]', m.text()));
await page.goto(URL);
await page.click('.call-start', { timeout: 20000 });
const t0 = Date.now();
const levels = [];
let screen = '';
while (Date.now() - t0 < SECONDS * 1000) {
  const s = await page.evaluate(async () => {
    const pc = window.__pcs[window.__pcs.length - 1];
    let level = null, state = pc ? pc.connectionState : 'none';
    if (pc) (await pc.getStats()).forEach((r) => { if (r.type === 'inbound-rtp' && r.kind === 'audio') level = r.audioLevel ?? null; });
    return { level, state, text: document.querySelector('.call-screen, [class*="call"]')?.innerText?.slice(0, 200) ?? '' };
  });
  levels.push({ at: Date.now(), level: s.level, state: s.state });
  if (s.text !== screen) { screen = s.text; console.log(`${((Date.now() - t0) / 1000).toFixed(1)}s screen: ${screen.replace(/\s+/g, ' ')}`); }
  await page.waitForTimeout(250);
}
writeFileSync('/audio/levels.json', JSON.stringify(levels));
await page.click('[aria-label="Hang up"]').catch(() => console.log('no hang-up button'));
await page.waitForTimeout(1000);
await browser.close();
