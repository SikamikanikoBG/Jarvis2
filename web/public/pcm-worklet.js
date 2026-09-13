// The microphone's samples, handed to the main thread a frame at a time (see src/voice/listener.ts).
// An AudioWorklet runs on the audio thread and must be a file of its own; this is that file.
// It does one thing: batch the 128-sample render quanta into ~43 ms frames and post them.
class PcmTap extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(2048);
    this.filled = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;
    let offset = 0;
    while (offset < channel.length) {
      const room = this.buffer.length - this.filled;
      const take = Math.min(room, channel.length - offset);
      this.buffer.set(channel.subarray(offset, offset + take), this.filled);
      this.filled += take;
      offset += take;
      if (this.filled === this.buffer.length) {
        // Transfer, not copy: the frame moves to the main thread and a fresh one is allocated.
        this.port.postMessage(this.buffer, [this.buffer.buffer]);
        this.buffer = new Float32Array(2048);
        this.filled = 0;
      }
    }
    return true;
  }
}

registerProcessor('pcm-tap', PcmTap);
