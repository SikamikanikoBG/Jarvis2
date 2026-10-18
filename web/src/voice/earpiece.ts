/**
 * Which microphone puts a call in the earpiece (2026-10-18).
 *
 * Chrome on Android opens a call's microphone on "Default" in speakerphone mode: the sound comes
 * from the loudspeaker. On a phone whose top earpiece is also a loudspeaker (stereo), that looks
 * like the earpiece in the hand - but pressed to the ear the proximity sensor reads "near" and the
 * phone mutes that speaker to protect the ear. Jarvis and the hold music vanished at the ear and
 * came back with the phone tilted off it, while the call's trace showed the audio arriving and
 * playing the whole time.
 *
 * Chrome lists its audio routes as microphones - "Speakerphone", "Headset earpiece", and any
 * headset - and opening one sets the route (speakerphone off for the earpiece). Their ids are
 * hashed per site, so the route is known by the name Chrome gives it: a platform constant, like a
 * MIME type, not words from a conversation. With anything else listed (a wired or Bluetooth
 * headset, a USB microphone) the default is left to choose, as the phone's own dialer would.
 */
const SPEAKERPHONE = 'Speakerphone';
const EARPIECE = 'Headset earpiece';

/** The deviceId of Chrome's earpiece route, or null when the default should decide. */
export function earpieceInput(devices: readonly MediaDeviceInfo[]): string | null {
  const inputs = devices.filter((d) => d.kind === 'audioinput' && d.deviceId !== 'default' && d.deviceId !== 'communications');
  const earpiece = inputs.find((d) => d.label === EARPIECE);
  if (!earpiece) return null;
  const other = inputs.some((d) => d.label !== EARPIECE && d.label !== SPEAKERPHONE);
  return other ? null : earpiece.deviceId;
}
