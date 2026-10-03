import { describe, expect, it } from 'vitest';
import type { MailItem } from '../protocol/types';
import { frameDocument } from './frame';
import { participants } from './people';

const mail = (over: Partial<MailItem>): MailItem => ({ entry_id: 'x', subject: 's', ...over });

describe('participants', () => {
  it('lists senders and every recipient once, by address', () => {
    const people = participants([
      mail({
        from: { name: 'Maria Petrova', address: 'mpetrova@postbank.bg' },
        recipients: [
          { name: 'Arsen Apostolov', address: 'aapostolov@postbank.bg', type: 'to' },
          { name: 'Ops', address: 'ops@postbank.bg', type: 'cc' },
        ],
      }),
      mail({
        from: { name: 'Arsen Apostolov', address: 'AApostolov@postbank.bg' },
        recipients: [{ name: 'Maria Petrova', address: 'mpetrova@postbank.bg', type: 'to' }],
      }),
    ]);
    expect(people).toEqual([
      { name: 'Maria Petrova', address: 'mpetrova@postbank.bg' },
      { name: 'Arsen Apostolov', address: 'aapostolov@postbank.bg' },
      { name: 'Ops', address: 'ops@postbank.bg' },
    ]);
  });

  it('falls back to the To / Cc names of an older host, without doubling a known person', () => {
    const people = participants([
      mail({ from: { name: 'Pete', address: 'pete@bank.bg' }, to: 'Maria; Pete', cc: 'Ivan' }),
    ]);
    expect(people).toEqual([
      { name: 'Pete', address: 'pete@bank.bg' },
      { name: 'Maria', address: '' },
      { name: 'Ivan', address: '' },
    ]);
  });
});

describe('frameDocument', () => {
  it('puts the no-script policy and the new-tab base into the head', () => {
    const doc = frameDocument('<html><head><title>t</title></head><body><p>Hi</p></body></html>');
    expect(doc.indexOf("script-src 'none'")).toBeLessThan(doc.indexOf('<title>'));
    expect(doc).toContain('<base target="_blank">');
    expect(frameDocument('<p>bare</p>')).toMatch(/^<!doctype html><html><head>.*<\/head><body><p>bare<\/p>/);
  });
});
