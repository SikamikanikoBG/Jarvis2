// Renders public/icons/icon-192.png and icon-512.png without any dependency (zlib is built in).
// Same mark as icon.svg: navy tile, cyan ring, centre dot, four ticks.
import { deflateSync } from 'node:zlib';
import { writeFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const out = join(here, '..', 'public', 'icons');
mkdirSync(out, { recursive: true });

const NAVY = [10, 14, 26];
const CYAN = [37, 216, 255];

function render(size) {
  const px = new Uint8Array(size * size * 4);
  const c = size / 2;
  const r = size * 0.265;
  const stroke = size * 0.055;
  const dot = size * 0.086;
  const tickLen = size * 0.094;
  const corner = size * 0.22;
  const inTile = (x, y) => {
    const dx = Math.max(Math.abs(x - c) - (c - corner), 0);
    const dy = Math.max(Math.abs(y - c) - (c - corner), 0);
    return dx * dx + dy * dy <= corner * corner;
  };
  const coverage = (fn, x, y) => {
    let hit = 0;
    for (let sy = 0; sy < 4; sy++) for (let sx = 0; sx < 4; sx++) if (fn(x + (sx + 0.5) / 4, y + (sy + 0.5) / 4)) hit++;
    return hit / 16;
  };
  const mark = (x, y) => {
    const d = Math.hypot(x - c, y - c);
    if (Math.abs(d - r) <= stroke / 2) return true;
    if (d <= dot) return true;
    const ax = Math.abs(x - c);
    const ay = Math.abs(y - c);
    const near = (a, b) => a <= stroke / 2 && b >= r + stroke * 1.6 && b <= r + stroke * 1.6 + tickLen;
    return near(ax, ay) || near(ay, ax);
  };
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const i = (y * size + x) * 4;
      const tile = coverage(inTile, x, y);
      const m = coverage(mark, x, y);
      const col = [0, 1, 2].map((k) => NAVY[k] * (1 - m) + CYAN[k] * m);
      px[i] = col[0];
      px[i + 1] = col[1];
      px[i + 2] = col[2];
      px[i + 3] = Math.round(255 * tile);
    }
  }
  return px;
}

const CRC_TABLE = new Int32Array(256).map((_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c;
});
function crc32(buf) {
  let c = -1;
  for (const b of buf) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ -1) >>> 0;
}
function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([len, body, crc]);
}
function png(size, rgba) {
  const raw = Buffer.alloc((size * 4 + 1) * size);
  for (let y = 0; y < size; y++) {
    raw[y * (size * 4 + 1)] = 0;
    raw.set(rgba.subarray(y * size * 4, (y + 1) * size * 4), y * (size * 4 + 1) + 1);
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // RGBA
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(raw, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

for (const size of [192, 512]) {
  const file = join(out, `icon-${size}.png`);
  writeFileSync(file, png(size, render(size)));
  console.log('wrote', file);
}
