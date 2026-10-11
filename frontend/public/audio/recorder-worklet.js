// Mic capture: AudioContext rate (44.1/48 kHz) -> 16 kHz mono 16-bit PCM in ~40 ms frames.
// Downsampling averages each output sample's input span (a box filter), which is enough anti-aliasing
// for speech. Also reports an RMS level for the UI about every 50 ms.

const TARGET_RATE = 16000;
const FRAME_SAMPLES = 640; // 40 ms at 16 kHz

class RecorderProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / TARGET_RATE;
    this.acc = 0;
    this.accCount = 0;
    this.phase = 0;
    this.frame = new Int16Array(FRAME_SAMPLES);
    this.index = 0;
    this.levelSum = 0;
    this.levelCount = 0;
    this.enabled = true;
    this.port.onmessage = (e) => {
      if (e.data?.type === "enable") this.enabled = !!e.data.value;
    };
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;
    for (let i = 0; i < channel.length; i++) {
      const s = channel[i];
      this.levelSum += s * s;
      this.levelCount++;
      this.acc += s;
      this.accCount++;
      this.phase += 1;
      if (this.phase >= this.ratio) {
        this.phase -= this.ratio;
        const v = Math.max(-1, Math.min(1, this.acc / this.accCount));
        this.acc = 0;
        this.accCount = 0;
        this.frame[this.index++] = v < 0 ? v * 0x8000 : v * 0x7fff;
        if (this.index === FRAME_SAMPLES) {
          if (this.enabled) this.port.postMessage({ type: "audio", buffer: this.frame.buffer }, [this.frame.buffer]);
          this.frame = new Int16Array(FRAME_SAMPLES);
          this.index = 0;
        }
      }
    }
    if (this.levelCount >= sampleRate / 20) {
      this.port.postMessage({ type: "level", value: Math.sqrt(this.levelSum / this.levelCount) });
      this.levelSum = 0;
      this.levelCount = 0;
    }
    return true;
  }
}

registerProcessor("recorder-processor", RecorderProcessor);
