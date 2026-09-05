// Diagnoses the autoscroll: prints transcript geometry after each step of the confirm flow.
import { chromium } from '@playwright/test';

const base = process.argv[2] ?? 'http://localhost:5174';
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 }, colorScheme: 'dark' });
page.on('console', (m) => {
  if (m.text().startsWith('[transcript]')) console.log('   ', m.text());
});
const geo = async (label) => {
  const g = await page.evaluate(() => {
    const el = document.querySelector('.transcript');
    return el
      ? { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight, distance: el.scrollHeight - el.scrollTop - el.clientHeight, pill: Boolean(document.querySelector('.jump')) }
      : null;
  });
  console.log(label.padEnd(28), JSON.stringify(g));
};
await page.goto(base);
await page.waitForSelector('.conv');
await page.click('.sidebar-head .icon-btn'); // fresh conversation every run
await page.fill('textarea', 'hello');
await page.keyboard.press('Enter');
await page.waitForFunction(() => document.querySelectorAll('.runchip').length >= 1 && !document.querySelector('.runchip .chip-accent'), null, { timeout: 40000 });
await geo('opened');
await page.fill('textarea', 'Show me the flagged mail (tools)');
await page.keyboard.press('Enter');
await page.waitForTimeout(500);
await geo('sent');
await page.waitForFunction(() => document.querySelectorAll('.runchip').length >= 2 && !document.querySelectorAll('.runchip')[1].querySelector('.chip-accent'), null, { timeout: 40000 });
await page.waitForTimeout(300);
await geo('done');
await page.click('.tool >> nth=0 >> summary');
await page.waitForTimeout(300);
await geo('card expanded');
await page.click('.runchip >> nth=-1');
await page.waitForSelector('.inspector .tl');
await page.waitForTimeout(400);
await geo('inspector open');
await page.click('.inspector-head .icon-btn');
await page.waitForTimeout(400);
await geo('inspector closed');
await page.fill('textarea', 'Reply to Rumen and confirm before sending');
await geo('filled');
await page.keyboard.press('Enter');
await page.waitForTimeout(150);
await geo('sent confirm +150ms');
await page.waitForTimeout(600);
await geo('sent confirm +750ms');
await page.waitForSelector('.confirm', { timeout: 20000 });
await page.waitForTimeout(300);
await geo('confirm card');
await browser.close();
