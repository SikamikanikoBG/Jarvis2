// Headless call against the bot: Chromium's fake microphone plays test/question.wav (silence,
// a question, silence, a cut-in, silence). The verdict is read from the bot's trace afterwards
// (check_trace.py). Headless Chromium has no loudspeaker-to-microphone path, so this proves the
// pipeline, the music and barge-in, never that the phone does not hear itself.
//
//   docker run --rm --network host -v $PWD:/work -w /work mcr.microsoft.com/playwright:v1.63.0-noble \
//     sh -c 'npm i --no-save --silent playwright@1.63.0 && node test/call_check.mjs'
import { writeFileSync } from 'node:fs';
import { chromium } from 'playwright';

const URL = process.env.CALL_URL || 'http://127.0.0.1:9150/';
const SECONDS = Number(process.env.CALL_SECONDS || 60);

const browser = await chromium.launch({
  args: [
    '--use-fake-ui-for-media-stream',
    '--use-fake-device-for-media-stream',
    '--use-file-for-fake-audio-capture=/work/test/question.wav',
    '--autoplay-policy=no-user-gesture-required',
    ...(process.env.CALL_RESOLVE ? ['--host-resolver-rules=' + process.env.CALL_RESOLVE] : []),
  ],
});
const page = await browser.newPage();
page.on('console', (m) => console.log('[page]', m.text()));
await page.goto(URL);
await page.click('#call');
await page.waitForFunction(() => document.getElementById('state').textContent === 'Listening', null, { timeout: 20000 });
console.log('connected');
// The far end's level as the browser receives it, every 250 ms: proof that the music and the
// voice are on the call's track, not only in the bot's trace.
const levels = [];
const end = Date.now() + SECONDS * 1000;
while (Date.now() < end) {
  const level = await page.evaluate(async () => {
    let v = null;
    (await pc.getStats()).forEach((s) => { if (s.type === 'inbound-rtp' && s.kind === 'audio') v = s.audioLevel ?? null; });
    return v;
  });
  levels.push({ at: Date.now(), level });
  await page.waitForTimeout(250);
}
writeFileSync('test/levels.json', JSON.stringify(levels));
console.log('--- diagnostics ---\n' + (await page.innerText('#diag')));
console.log('--- page log (newest first) ---\n' + (await page.innerText('#log')));
await page.click('#hang');
await page.waitForTimeout(1500);
await browser.close();
