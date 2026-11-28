/**
 * This device's position for Jarvis (core features/location.py).
 *
 * Opt-in per device: nothing is asked or sent until he switches "Share this device's location"
 * on in Settings, which is when the browser asks for permission. While it is on and the app is in
 * front, the phone watches its position; each message carries the newest fix, and a fix is also
 * posted on its own when he has moved ~100 m or ten minutes have passed - so a scheduled run
 * knows where he was last seen even if he only opened the app. Hidden app = no watching (the
 * battery, and a web page cannot anyway). A private chat never carries a fix.
 */
import { api } from '../api/client';
import type { DeviceLocation } from '../protocol/types';

const KEY = 'jarvis.location.share';
/** A fix older than this is not attached to a message: the core already has it, or better. */
const ATTACH_MAX_AGE_MS = 30 * 60_000;
const POST_MOVED_M = 100;
const POST_EVERY_MS = 10 * 60_000;

let latest: DeviceLocation | null = null;
let lastPosted: DeviceLocation | null = null;
let lastPostedAt = 0;
let watchId: number | null = null;
let listening = false;

export function sharingEnabled(): boolean {
  try {
    return localStorage.getItem(KEY) === 'on';
  } catch {
    return false;
  }
}

function supported(): boolean {
  return typeof navigator !== 'undefined' && 'geolocation' in navigator;
}

/** Metres between two fixes (haversine). */
export function distanceM(a: { lat: number; lon: number }, b: { lat: number; lon: number }): number {
  const r = 6_371_008.8;
  const rad = Math.PI / 180;
  const dp = (b.lat - a.lat) * rad;
  const dl = (b.lon - a.lon) * rad;
  const h = Math.sin(dp / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dl / 2) ** 2;
  return 2 * r * Math.asin(Math.sqrt(h));
}

/** Whether a new fix is worth its own post: moved far enough, or long enough since the last one. */
export function shouldPost(fix: DeviceLocation, prev: DeviceLocation | null, prevAt: number, now: number): boolean {
  if (!prev) return true;
  return distanceM(fix, prev) >= POST_MOVED_M || now - prevAt >= POST_EVERY_MS;
}

function fromPosition(p: GeolocationPosition): DeviceLocation {
  const speed = p.coords.speed;
  return {
    lat: p.coords.latitude,
    lon: p.coords.longitude,
    accuracy_m: Number.isFinite(p.coords.accuracy) ? p.coords.accuracy : null,
    taken_at: new Date(p.timestamp).toISOString(),
    source: 'browser',
    speed_mps: speed !== null && Number.isFinite(speed) && speed >= 0 ? speed : null,
  };
}

function onFix(p: GeolocationPosition) {
  latest = fromPosition(p);
  const now = Date.now();
  if (shouldPost(latest, lastPosted, lastPostedAt, now)) {
    lastPosted = latest;
    lastPostedAt = now;
    void api.location.post(latest).catch(() => {
      // offline: the next message or fix carries it
      lastPostedAt = 0;
    });
  }
}

function start() {
  if (watchId !== null || !supported() || !sharingEnabled()) return;
  watchId = navigator.geolocation.watchPosition(onFix, () => undefined, {
    enableHighAccuracy: true,
    maximumAge: 60_000,
    timeout: 30_000,
  });
}

function stop() {
  if (watchId !== null && supported()) navigator.geolocation.clearWatch(watchId);
  watchId = null;
}

function onVisibility() {
  if (document.visibilityState === 'visible') start();
  else stop();
}

/** Called once at start-up: watch while the app is in front and sharing is on. */
export function initLocation(): void {
  if (listening || typeof document === 'undefined') return;
  listening = true;
  document.addEventListener('visibilitychange', onVisibility);
  onVisibility();
}

/**
 * Switch sharing on or off for this device. On: asks the browser for permission (the prompt
 * appears now, from his tap) and resolves to whether it was granted.
 */
export function setSharing(on: boolean): Promise<boolean> {
  try {
    localStorage.setItem(KEY, on ? 'on' : 'off');
  } catch {
    // storage blocked: sharing lasts until the app closes
  }
  if (!on) {
    stop();
    latest = null;
    return Promise.resolve(false);
  }
  if (!supported()) return Promise.resolve(false);
  return new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      (p) => {
        onFix(p);
        start();
        resolve(true);
      },
      () => {
        try {
          localStorage.setItem(KEY, 'off');
        } catch {
          // nothing to undo
        }
        resolve(false);
      },
      { enableHighAccuracy: true, timeout: 20_000, maximumAge: 60_000 },
    );
  });
}

/** What a message carries: the newest fix, unless sharing is off, it is stale, or the chat is private. */
export function deviceLocation(incognito: boolean): { location?: DeviceLocation } {
  if (incognito || !latest || !sharingEnabled()) return {};
  if (Date.now() - Date.parse(latest.taken_at) > ATTACH_MAX_AGE_MS) return {};
  return { location: latest };
}
