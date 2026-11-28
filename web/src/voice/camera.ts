/**
 * The camera on a call (2026-11-28: "show him what is in front of me and ask about it").
 *
 * Not a video stream to the model - the model on vader reads pictures, not video, and a picture a
 * second would drown it. The camera runs on the phone with a preview; when he finishes saying
 * something, the frame of that moment is taken and uploaded while his words are transcribed, and
 * it goes with the turn. So "what is this?" sends what he was pointing at as he said it.
 */

export type Facing = 'environment' | 'user';

/** Longest side of a frame sent to the model: enough to read a label, small enough to be quick. */
const MAX_SIDE = 1024;
const QUALITY = 0.72;

export class CallCamera {
  stream: MediaStream | null = null;
  facing: Facing = 'environment';
  private video: HTMLVideoElement | null = null;

  static supported(): boolean {
    return typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getUserMedia;
  }

  async start(facing: Facing = this.facing): Promise<MediaStream> {
    this.stop();
    this.facing = facing;
    this.stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: facing }, width: { ideal: 1920 }, height: { ideal: 1080 } },
      audio: false,
    });
    // A detached element of our own to read frames from, whatever the screen shows.
    const v = document.createElement('video');
    v.muted = true;
    v.playsInline = true;
    v.srcObject = this.stream;
    await v.play().catch(() => undefined);
    this.video = v;
    return this.stream;
  }

  async flip(): Promise<MediaStream> {
    return this.start(this.facing === 'environment' ? 'user' : 'environment');
  }

  stop(): void {
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    if (this.video) this.video.srcObject = null;
    this.video = null;
  }

  /** The frame of this moment as a JPEG, or null when there is no picture yet. */
  async frame(): Promise<Blob | null> {
    const v = this.video;
    if (!v || v.videoWidth === 0 || v.videoHeight === 0) return null;
    const scale = Math.min(1, MAX_SIDE / Math.max(v.videoWidth, v.videoHeight));
    const w = Math.round(v.videoWidth * scale);
    const h = Math.round(v.videoHeight * scale);
    const canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext('2d');
    if (!ctx) return null;
    ctx.drawImage(v, 0, 0, w, h);
    return new Promise((resolve) => canvas.toBlob((b) => resolve(b), 'image/jpeg', QUALITY));
  }
}

/** Scale (w, h) so its longest side is at most `max` - exported for the test. */
export function fitSide(w: number, h: number, max = MAX_SIDE): [number, number] {
  const s = Math.min(1, max / Math.max(w, h));
  return [Math.round(w * s), Math.round(h * s)];
}
