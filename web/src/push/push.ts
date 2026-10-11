/**
 * This device as one Jarvis can ring (docs/stories/11_call_me.md): the browser's web push
 * subscription, handed to the core. The ring itself arrives in the service worker (sw.js).
 *
 * Asking for the permission needs a tap (iOS refuses otherwise, and an unasked prompt is
 * rude anyway), so `enablePush` is called from one: the bell, or the button in Settings. At
 * boot `refreshPush` only re-sends a subscription the browser already has, never prompts.
 */

import { api } from '../api/client';

export type PushStatus = 'unsupported' | 'blocked' | 'off' | 'on';

export function pushSupported(): boolean {
  return typeof window !== 'undefined' && 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
}

/** Whether this device rings, without asking anything. */
export async function pushStatus(): Promise<PushStatus> {
  if (!pushSupported()) return 'unsupported';
  if (Notification.permission === 'denied') return 'blocked';
  const reg = await navigator.serviceWorker.getRegistration();
  const sub = await reg?.pushManager.getSubscription();
  return sub && Notification.permission === 'granted' ? 'on' : 'off';
}

/** The endpoint this browser rings at, to tell it apart in the device list. */
export async function thisEndpoint(): Promise<string | null> {
  if (!pushSupported()) return null;
  const reg = await navigator.serviceWorker.getRegistration();
  return (await reg?.pushManager.getSubscription())?.endpoint ?? null;
}

/** Turn ringing on for this device. Call it from a tap. Resolves to how it ended. */
export async function enablePush(): Promise<PushStatus> {
  if (!pushSupported()) return 'unsupported';
  // Inside the tap, before anything is awaited: the prompt must belong to the gesture.
  const permission = await Notification.requestPermission();
  if (permission !== 'granted') return permission === 'denied' ? 'blocked' : 'off';
  const reg = await navigator.serviceWorker.ready;
  const { public_key } = await api.push.state();
  let sub = await reg.pushManager.getSubscription();
  // A subscription made for another key (the core's key was replaced) cannot be pushed to.
  if (sub && !sameKey(sub, public_key)) {
    await sub.unsubscribe();
    sub = null;
  }
  sub ??= await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: fromB64url(public_key) });
  await register(sub);
  return 'on';
}

/** At boot: tell the core again about a subscription this browser already has (no prompt). */
export async function refreshPush(): Promise<void> {
  if (!pushSupported() || Notification.permission !== 'granted') return;
  const reg = await navigator.serviceWorker.getRegistration();
  const sub = await reg?.pushManager.getSubscription();
  if (sub) await register(sub);
}

/** Stop ringing this device. */
export async function disablePush(): Promise<void> {
  if (!pushSupported()) return;
  const reg = await navigator.serviceWorker.getRegistration();
  const sub = await reg?.pushManager.getSubscription();
  if (!sub) return;
  await api.push.forget(sub.endpoint).catch(() => undefined);
  await sub.unsubscribe();
}

async function register(sub: PushSubscription): Promise<void> {
  const json = sub.toJSON();
  const p256dh = json.keys?.p256dh;
  const auth = json.keys?.auth;
  if (!json.endpoint || !p256dh || !auth) throw new Error('The browser gave an incomplete push subscription.');
  await api.push.add({ endpoint: json.endpoint, keys: { p256dh, auth }, name: deviceName() });
}

function sameKey(sub: PushSubscription, publicKey: string): boolean {
  const key = sub.options.applicationServerKey;
  if (!key) return true; // the browser does not say; assume it is ours
  const want = fromB64url(publicKey);
  const have = new Uint8Array(key);
  return have.length === want.length && have.every((b, i) => b === want[i]);
}

function fromB64url(text: string): Uint8Array<ArrayBuffer> {
  const b64 = (text + '='.repeat((4 - (text.length % 4)) % 4)).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(b64);
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

/** A name to tell the devices apart in Settings: "Android · Brave", "Windows · Edge". */
export function deviceName(ua = navigator.userAgent): string {
  const os = ua.includes('Android') ? 'Android' : /iPhone|iPad/.test(ua) ? 'iPhone' : ua.includes('Windows') ? 'Windows' : ua.includes('Mac OS') ? 'Mac' : ua.includes('Linux') ? 'Linux' : 'Device';
  const brave = (navigator as Navigator & { brave?: unknown }).brave !== undefined;
  const browser = brave ? 'Brave' : ua.includes('Edg/') ? 'Edge' : ua.includes('Firefox/') ? 'Firefox' : ua.includes('Chrome/') ? 'Chrome' : ua.includes('Safari/') ? 'Safari' : '';
  return browser ? `${os} · ${browser}` : os;
}
