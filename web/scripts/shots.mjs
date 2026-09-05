// Visual smoke run against `npm run mock` + a dev server: walks every screen and flow and
// saves screenshots to scripts/shots/. Uses the system Edge/Chrome so nothing is downloaded.
//   JARVIS_BACKEND=http://127.0.0.1:9021 npx vite --port 5174   (in one terminal)
//   node scripts/shots.mjs http://localhost:5174                 (in another)
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const base = process.argv[2] ?? 'http://localhost:5174';
const only = process.argv[3] ?? null; // run a single session by name
const out = join(dirname(fileURLToPath(import.meta.url)), 'shots');
mkdirSync(out, { recursive: true });

const browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL ?? 'msedge', headless: true });
const errors = [];

async function session(name, viewport, colorScheme, steps) {
  if (only && only !== name) return;
  const ctx = await browser.newContext({ viewport, colorScheme, deviceScaleFactor: 1, hasTouch: viewport.width < 900 });
  const page = await ctx.newPage();
  page.on('pageerror', (e) => errors.push(`${name}: ${e.message}`));
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(`${name} console: ${m.text()}`);
  });
  const shot = async (label) => {
    await page.screenshot({ path: join(out, `${name}-${label}.png`) });
    console.log('shot', `${name}-${label}`);
  };
  try {
    await steps(page, shot);
  } catch (e) {
    errors.push(`${name} step failed: ${String(e.message).split('\n')[0]}`);
    console.log('FAILED', name, String(e.message).split('\n')[0]);
    await shot('FAILED');
  }
  await ctx.close();
}

const desktop = { width: 1400, height: 900 };
const mobile = { width: 390, height: 844 };

await session('desktop-dark', desktop, 'dark', async (page, shot) => {
  await page.goto(base);
  await page.waitForSelector('.conv');
  await shot('01-empty');
  await page.click('.conv:has-text("Morning planning")');
  await page.waitForSelector('.msg-bot');
  await shot('02-conversation');
  // stream a tools flow
  await page.fill('textarea', 'Show me the flagged mail (tools)');
  await page.keyboard.press('Enter');
  await page.waitForSelector('.msg-bot.streaming, .reasoning', { timeout: 8000 });
  await page.waitForTimeout(900);
  await shot('03-streaming');
  await page.waitForSelector('.tool', { timeout: 20000 });
  await page.waitForTimeout(600);
  await shot('04-tools');
  await page.waitForSelector('.runchip:not(:has(.chip-accent))', { timeout: 40000 });
  await page.waitForTimeout(300);
  await shot('05-done');
  await page.click('.tool >> nth=0 >> summary');
  await page.click('.runchip >> nth=-1');
  await page.waitForSelector('.inspector .tl');
  await page.waitForTimeout(400);
  await shot('06-inspector');
  await page.click('.inspector .tl-head >> nth=2');
  await page.waitForTimeout(200);
  await shot('07-inspector-payload');
  await page.click('.inspector-head .icon-btn');
  // confirm flow
  await page.fill('textarea', 'Reply to Rumen and confirm before sending');
  await page.keyboard.press('Enter');
  await page.waitForSelector('.confirm', { timeout: 20000 });
  await page.waitForTimeout(300);
  await shot('08-confirm');
  await page.fill('.confirm input', 'add the deck link');
  await page.click('.confirm .btn-primary');
  await page.waitForSelector('.runchip:not(:has(.chip-accent))', { timeout: 40000 });
  await page.waitForTimeout(300);
  await shot('09-confirmed');
  // cancel flow
  await page.fill('textarea', 'long answer please');
  await page.keyboard.press('Enter');
  await page.waitForSelector('.msg-bot.streaming', { timeout: 20000 });
  await page.waitForTimeout(1500);
  await page.click('.stop-btn');
  await page.waitForSelector('.partial-mark', { timeout: 15000 });
  await page.waitForTimeout(300);
  await shot('10-stopped');
  // judge flow
  await page.fill('textarea', 'judge me');
  await page.keyboard.press('Enter');
  await page.waitForSelector('.note-error', { timeout: 60000 });
  await page.waitForTimeout(400);
  await shot('11-judge');
  // sidebar folders + menu
  await page.click('.folder-head >> nth=0');
  await page.click('.folder-head >> nth=1');
  await page.hover('.conv >> nth=0');
  await page.click('.conv >> nth=0 >> .conv-more');
  await page.waitForTimeout(200);
  await shot('12-sidebar-menu');
  await page.keyboard.press('Escape');
  // settings
  await page.click('.nav-link >> text=Settings');
  await page.waitForSelector('.role-card');
  await shot('13-settings');
  await page.evaluate(() => document.querySelector('.think-row')?.scrollIntoView({ block: 'start' }));
  await page.waitForTimeout(200);
  await shot('13b-settings-think-mcp');
  await page.click('.mcp-row >> nth=0 >> .icon-btn >> nth=0');
  await page.waitForTimeout(200);
  await page.evaluate(() => document.querySelector('.mcp-form')?.scrollIntoView({ block: 'center' }));
  await shot('13c-settings-mcp-edit');
  await page.click('.mcp-form .btn-ghost');
  // native min/max validation stops obviously bad numbers client-side; a bad timezone reaches the server → 422
  await page.fill('input[placeholder="Europe/Sofia"]', 'Mars');
  await page.waitForTimeout(200);
  const saveResponse = page.waitForResponse((r) => r.url().includes('/api/settings') && r.request().method() === 'PATCH');
  await page.click('.savebar .btn-primary');
  const res = await saveResponse;
  console.log('settings PATCH →', res.status(), (await res.text()).slice(0, 200));
  await page.waitForTimeout(300);
  await page.evaluate(() => document.querySelector('.field-error')?.scrollIntoView({ block: 'center' }));
  await shot('14-settings-422');
  // status
  await page.click('.nav-link >> text=Status');
  await page.waitForSelector('.ep');
  await page.waitForTimeout(400);
  await shot('15-status');
  await page.click('.tools-list > summary');
  await page.waitForTimeout(200);
  await page.evaluate(() => document.querySelector('.tools-list')?.scrollIntoView({ block: 'start' }));
  await shot('16-status-tools');
});

await session('desktop-light', desktop, 'light', async (page, shot) => {
  await page.goto(base);
  await page.waitForSelector('.conv');
  await page.click('.conv:has-text("Deck numbers")'); // an empty conversation
  await page.waitForSelector('textarea:not([disabled])');
  await page.fill('textarea', 'tools please');
  await page.keyboard.press('Enter');
  await page.waitForSelector('.runchip:not(:has(.chip-accent))', { timeout: 40000 });
  await page.waitForTimeout(300);
  await shot('01-conversation');
  await page.click('.nav-link >> text=Status');
  await page.waitForSelector('.ep');
  await page.waitForTimeout(300);
  await shot('02-status');
  await page.click('.nav-link >> text=Settings');
  await page.waitForSelector('.role-card');
  await shot('03-settings');
});

await session('mobile-dark', mobile, 'dark', async (page, shot) => {
  await page.goto(base);
  await page.waitForSelector('.bottomnav');
  await shot('01-empty');
  await page.click('.topbar .icon-btn >> nth=0');
  await page.waitForSelector('.drawer .conv');
  await shot('02-drawer');
  await page.click('.drawer .conv:has-text("Morning planning")');
  await page.waitForSelector('.msg-bot');
  await shot('03-conversation');
  await page.fill('textarea', 'confirm sending');
  await page.click('.send-btn');
  await page.waitForSelector('.confirm', { timeout: 20000 });
  await page.waitForTimeout(300);
  await shot('04-confirm');
  await page.click('.confirm .btn-secondary');
  await page.waitForSelector('.runchip:not(:has(.chip-accent))', { timeout: 40000 });
  await page.waitForTimeout(300);
  await shot('05-rejected');
  await page.click('.runchip >> nth=-1');
  await page.waitForSelector('.drawer-sheet .tl');
  await page.waitForTimeout(400);
  await shot('06-inspector-sheet');
  await page.keyboard.press('Escape');
  await page.click('.bottomnav button >> nth=1');
  await page.waitForSelector('.runitem');
  await shot('07-runs');
  await page.click('.bottomnav button >> nth=3');
  await page.waitForSelector('.ep');
  await page.waitForTimeout(300);
  await shot('08-status');
  await page.click('.bottomnav button >> nth=2');
  await page.waitForSelector('.role-card');
  await shot('09-settings');
});

await session('features', desktop, 'dark', async (page, shot) => {
  await page.goto(`${base}/boards`);
  await page.waitForSelector('.board-col');
  await shot('01-boards');
  await page.click('.note-card >> nth=0 >> .icon-btn >> nth=0');
  await page.waitForTimeout(200);
  await shot('02-boards-note-tools');
  await page.goto(`${base}/knowledge`);
  await page.waitForSelector('.kg-entity');
  await page.click('.kg-entity >> nth=0');
  await page.waitForSelector('.kg-graph');
  await page.waitForTimeout(300);
  await shot('03-knowledge');
  await page.goto(`${base}/skills`);
  await page.waitForSelector('.skill-row');
  await page.click('.skill-row >> nth=0 >> .skill-main');
  await page.waitForSelector('.skill-text');
  await page.waitForTimeout(200);
  await shot('04-skills-editor');
  await page.goto(`${base}/schedules`);
  await page.waitForSelector('.sched-row');
  await page.click('.sched-row >> nth=0 >> .icon-btn >> nth=2'); // fires
  await page.waitForTimeout(300);
  await shot('05-schedules');
  await page.click('text=New schedule');
  await page.waitForSelector('.sched-form');
  await shot('06-schedule-form');
  await page.goto(`${base}/meetings`);
  await page.waitForSelector('text=Start meeting');
  await page.click('text=Start meeting');
  await page.waitForSelector('.form-grid select');
  await page.fill('.form-grid input', 'Steering committee');
  await page.click('text=Start recording');
  await page.waitForSelector('.mtg-seg', { timeout: 15000 });
  await page.waitForTimeout(2800);
  await shot('07-meeting-live');
  await page.click('.kg-detail .btn-danger');
  await page.waitForTimeout(600);
  await shot('08-meeting-summarising');
  await page.goto(`${base}/triage`);
  await page.waitForSelector('.triage-table');
  await shot('09-triage');
  await page.goto(`${base}/status`);
  await page.waitForSelector('.ep');
  await page.click('text=Pair a phone');
  await page.waitForSelector('.qr svg');
  await shot('10-pairing');
  await page.goto(`${base}/settings`);
  await page.waitForSelector('.role-card');
  await page.evaluate(() => [...document.querySelectorAll('h2')].find((h) => h.textContent === 'Triage')?.scrollIntoView({ block: 'start' }));
  await page.waitForTimeout(200);
  await shot('11-settings-triage-collab');
  // chat: plan checklist + pin + summary divider
  await page.goto(base);
  await page.waitForSelector('.conv');
  await page.click('.conv:has-text("Morning planning")');
  await page.waitForSelector('.msg-bot');
  await page.waitForTimeout(400);
  await shot('12-chat-summary-divider');
  await page.fill('textarea', 'Make a plan for the deck');
  await page.keyboard.press('Enter');
  await page.waitForSelector('.plan', { timeout: 15000 });
  await page.waitForTimeout(2500);
  await shot('13-chat-plan');
  await page.waitForSelector('.runchip:not(:has(.chip-accent)) >> nth=-1', { timeout: 40000 });
  await page.hover('.msg-bot >> nth=-1');
  await page.click('.msg-bot >> nth=-1 >> .msg-action');
  await page.waitForSelector('.menu');
  await shot('14-chat-pin-picker');
});

await session('features-mobile', mobile, 'dark', async (page, shot) => {
  await page.goto(`${base}/boards`);
  await page.waitForSelector('.board-col');
  await shot('01-boards');
  await page.click('.bottomnav button >> nth=4');
  await page.waitForSelector('.more-grid');
  await shot('02-more');
  await page.click('.more-item >> nth=1');
  await page.waitForSelector('.kg-entity');
  await page.click('.kg-entity >> nth=0');
  await page.waitForSelector('.kg-graph');
  await page.waitForTimeout(300);
  await shot('03-knowledge-detail');
});

await session('panel', { width: 420, height: 760 }, 'dark', async (page, shot) => {
  await page.goto(`${base}/?mode=panel`);
  await page.waitForSelector('textarea');
  await page.fill('textarea', 'hello from the side panel');
  await page.click('.send-btn'); // coarse pointer: Enter is a newline, the button sends
  await page.waitForSelector('.runchip:not(:has(.chip-accent))', { timeout: 40000 });
  await page.waitForTimeout(300);
  await shot('01-panel');
});

await browser.close();
if (errors.length) {
  console.log('\nBROWSER ERRORS:');
  for (const e of errors) console.log(' -', e);
  process.exitCode = 1;
} else {
  console.log('\nno browser errors');
}
