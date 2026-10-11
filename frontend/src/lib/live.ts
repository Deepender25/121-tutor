// LiveClient: one voice conversation with the server's Gemini Live bridge.
//   mic (any rate) -> worklet -> 16 kHz PCM16 -> WebSocket -> server -> Gemini
//   Gemini -> server -> 24 kHz PCM16 -> scheduled AudioBuffers -> speakers
// Events from the server (transcripts, tool activity, status) are passed to `onMessage`.
// The WebSocket goes to this origin's /api, which Next.js proxies to the Python server.
import { apiWsUrl } from "./api";
import type { ToolUI } from "./types";

const OUTPUT_RATE = 24000;
const WORKLET_URL = "/audio/recorder-worklet.js";

export type LiveState = "idle" | "connecting" | "ready" | "reconnecting" | "closed";

export type LiveMessage =
  | { type: "transcript"; role: "user" | "model"; text: string }
  | { type: "tool_call"; id: string; name: string; args?: Record<string, unknown> }
  | { type: "tool_result"; id: string; name: string; ui?: ToolUI | null; error?: string }
  | { type: "turn_complete" }
  | { type: "interrupted" }
  | { type: "notice"; text: string }
  | { type: "status"; state: string }
  | { type: "error"; message: string; fatal?: boolean };

interface Handlers {
  onMessage?: (msg: LiveMessage) => void;
  onState?: (state: LiveState) => void;
  onLevel?: (level: number) => void;
  onError?: (message: string, fatal: boolean) => void;
}

export class LiveClient {
  notebookId: string;
  state: LiveState = "idle";
  micOn = false;
  speakerMuted = false;
  private h: Handlers;
  private ws: WebSocket | null = null;
  private ctx: AudioContext | null = null;
  private gain: GainNode | null = null;
  private analyser: AnalyserNode | null = null;
  private stream: MediaStream | null = null;
  private micSource: MediaStreamAudioSourceNode | null = null;
  private recorder: AudioWorkletNode | null = null;
  private micLevel = 0;
  private sources = new Set<AudioBufferSourceNode>();
  private bus: GainNode | null = null; // the current reply's audio, faded out (not cut) when interrupted
  private carry: Uint8Array | null = null; // odd trailing byte of a chunk: the next chunk's first sample half
  private nextTime = 0;
  private pendingText: string[] = [];
  private raf: number | null = null;

  constructor(notebookId: string, handlers: Handlers) {
    this.notebookId = notebookId;
    this.h = handlers;
  }

  get active() {
    return this.state === "connecting" || this.state === "ready" || this.state === "reconnecting";
  }

  get speaking() {
    return !!this.ctx && this.sources.size > 0;
  }

  // Seconds of reply audio queued but not heard yet. Text and tool results that arrive now belong to
  // speech that will be heard this much later (Gemini generates faster than real time).
  get playbackLag() {
    return this.ctx && this.sources.size ? Math.max(0, this.nextTime - this.ctx.currentTime) : 0;
  }

  // ---- connection -------------------------------------------------------------------

  async start({ mic = true } = {}) {
    if (this.active) {
      if (mic && !this.micOn) await this.enableMic();
      return;
    }
    this.setState("connecting");
    try {
      const Ctx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      this.ctx = new Ctx();
      if (this.ctx.state === "suspended") await this.ctx.resume();
      this.gain = this.ctx.createGain();
      this.gain.gain.value = this.speakerMuted ? 0 : 1;
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 512;
      this.gain.connect(this.analyser);
      this.analyser.connect(this.ctx.destination);
      this.levelLoop();
    } catch {
      this.fail("Your browser couldn't start audio. Try Chrome or Edge.");
      return;
    }
    if (mic) {
      const ok = await this.enableMic();
      if (!ok) { this.end(); return; }
    }
    this.openSocket();
  }

  private openSocket() {
    const ws = new WebSocket(apiWsUrl(`/api/notebooks/${this.notebookId}/live`));
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => {
      for (const text of this.pendingText.splice(0)) ws.send(JSON.stringify({ type: "text", text }));
    };
    ws.onmessage = (e) => {
      if (typeof e.data !== "string") { this.play(e.data as ArrayBuffer); return; }
      let msg: LiveMessage;
      try { msg = JSON.parse(e.data); } catch { return; }
      if (msg.type === "status") {
        if (msg.state === "ready") this.setState("ready");
        else if (msg.state === "reconnecting" || msg.state === "connecting") this.setState(msg.state);
        else if (msg.state === "idle") this.h.onMessage?.({ type: "notice", text: "Voice session paused after a few quiet minutes. Tap the orb to continue." });
      } else if (msg.type === "interrupted") {
        this.flushAudio();
      } else if (msg.type === "error") {
        this.h.onError?.(msg.message, !!msg.fatal);
      }
      this.h.onMessage?.(msg);
    };
    ws.onclose = () => {
      if (this.ws === ws) this.teardown();
    };
    ws.onerror = () => {
      if (this.state === "connecting") this.h.onError?.("Couldn't connect to the voice server.", true);
    };
  }

  end() {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try { this.ws.send(JSON.stringify({ type: "close" })); } catch { /* closing anyway */ }
    }
    const ws = this.ws;
    this.ws = null;
    try { ws?.close(); } catch { /* ignore */ }
    this.teardown();
  }

  private teardown() {
    this.disableMic(true);
    this.flushAudio();
    if (this.raf !== null) cancelAnimationFrame(this.raf);
    this.raf = null;
    if (this.ctx) { this.ctx.close().catch(() => {}); this.ctx = null; }
    this.ws = null;
    this.pendingText = [];
    this.setState("idle");
    this.h.onLevel?.(0);
  }

  private setState(state: LiveState) {
    if (this.state === state) return;
    this.state = state;
    this.h.onState?.(state);
  }

  private fail(message: string) {
    this.h.onError?.(message, true);
    this.setState("idle");
  }

  // ---- microphone -------------------------------------------------------------------

  async enableMic(): Promise<boolean> {
    if (this.micOn) return true;
    if (!navigator.mediaDevices?.getUserMedia) {
      this.h.onError?.("Microphone isn't available here. Open the app at http://127.0.0.1 in Chrome or Edge.", false);
      return false;
    }
    try {
      if (!this.stream) {
        if (!this.ctx) return false;
        this.stream = await navigator.mediaDevices.getUserMedia({
          audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
        await this.ctx.audioWorklet.addModule(WORKLET_URL);
        this.micSource = this.ctx.createMediaStreamSource(this.stream);
        this.recorder = new AudioWorkletNode(this.ctx, "recorder-processor");
        this.recorder.port.onmessage = (e) => {
          if (e.data.type === "level") this.micLevel = e.data.value;
          else if (e.data.type === "audio" && this.micOn && this.ws?.readyState === WebSocket.OPEN) {
            this.ws.send(e.data.buffer);
          }
        };
        this.micSource.connect(this.recorder);
        // The worklet must be pulled by the graph to run; route it through a muted gain.
        const sink = this.ctx.createGain();
        sink.gain.value = 0;
        this.recorder.connect(sink).connect(this.ctx.destination);
      }
      this.stream.getAudioTracks().forEach((t) => (t.enabled = true));
      this.recorder?.port.postMessage({ type: "enable", value: true });
      this.micOn = true;
      this.h.onState?.(this.state);
      return true;
    } catch (e) {
      const name = (e as Error)?.name;
      const blocked = name === "NotAllowedError" || name === "SecurityError";
      this.h.onError?.(blocked
        ? "Microphone access was blocked. Allow it in the address bar (lock icon), then try again."
        : "No microphone found. Plug one in or type your message instead.", false);
      return false;
    }
  }

  disableMic(release = false) {
    const wasOn = this.micOn;
    this.micOn = false;
    this.micLevel = 0;
    this.recorder?.port.postMessage({ type: "enable", value: false });
    if (wasOn && this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "audio_end" }));
    }
    if (release && this.stream) {
      this.stream.getTracks().forEach((t) => t.stop());
      this.stream = null;
      this.recorder = null;
      this.micSource = null;
    } else if (this.stream) {
      this.stream.getAudioTracks().forEach((t) => (t.enabled = false));
    }
    this.h.onState?.(this.state);
  }

  async toggleMic() {
    if (this.micOn) this.disableMic();
    else await this.enableMic();
  }

  // ---- text -------------------------------------------------------------------------

  async sendText(text: string) {
    if (!this.active) await this.start({ mic: false });
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: "text", text }));
    else this.pendingText.push(text);
  }

  // ---- playback ---------------------------------------------------------------------

  setSpeakerMuted(muted: boolean) {
    this.speakerMuted = muted;
    if (this.gain) this.gain.gain.value = muted ? 0 : 1;
  }

  private play(arrayBuffer: ArrayBuffer) {
    if (!this.ctx || !this.gain) return;
    // PCM16 samples are 2 bytes. A chunk with an odd length would shift every later sample by one byte,
    // which plays as loud noise, so a stray byte is held for the next chunk instead.
    let bytes = new Uint8Array(arrayBuffer);
    if (this.carry) {
      const joined = new Uint8Array(this.carry.length + bytes.length);
      joined.set(this.carry);
      joined.set(bytes, this.carry.length);
      bytes = joined;
      this.carry = null;
    }
    if (bytes.length % 2) {
      this.carry = bytes.slice(bytes.length - 1);
      bytes = bytes.subarray(0, bytes.length - 1);
    }
    if (!bytes.length) return;
    const pcm = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.length / 2);
    const buffer = this.ctx.createBuffer(1, pcm.length, OUTPUT_RATE);
    const data = buffer.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) data[i] = pcm[i] / 0x8000;
    const src = this.ctx.createBufferSource();
    src.buffer = buffer;
    if (!this.bus) {
      this.bus = this.ctx.createGain();
      this.bus.connect(this.gain);
    }
    src.connect(this.bus);
    const now = this.ctx.currentTime;
    // Small jitter buffer when starting from silence; back-to-back otherwise (gapless).
    if (this.nextTime < now) this.nextTime = now + 0.06;
    src.start(this.nextTime);
    this.nextTime += buffer.duration;
    this.sources.add(src);
    const wasSpeaking = this.sources.size === 1;
    src.onended = () => {
      this.sources.delete(src);
      if (this.sources.size === 0) this.h.onState?.(this.state);
    };
    if (wasSpeaking) this.h.onState?.(this.state);
  }

  // Stop the reply (interrupted, or the session ended). Cutting a waveform mid-cycle clicks, and repeated
  // interruptions turn that into chirps, so the reply fades out over 40 ms and is stopped just after.
  flushAudio() {
    const ctx = this.ctx;
    const bus = this.bus;
    this.bus = null;
    const stopAt = ctx ? ctx.currentTime + 0.05 : 0;
    if (ctx && bus && ctx.state === "running") {
      const t = ctx.currentTime;
      bus.gain.cancelScheduledValues(t);
      bus.gain.setValueAtTime(bus.gain.value, t);
      bus.gain.linearRampToValueAtTime(0, t + 0.04);
    }
    for (const src of this.sources) {
      src.onended = null;
      try { if (ctx && ctx.state === "running") src.stop(stopAt); else src.stop(); } catch { /* already stopped */ }
    }
    if (bus) setTimeout(() => { try { bus.disconnect(); } catch { /* closed with the context */ } }, 120);
    this.sources.clear();
    this.carry = null;
    this.nextTime = 0;
    this.h.onState?.(this.state);
  }

  private levelLoop() {
    const data = new Uint8Array(256);
    const tick = () => {
      if (!this.ctx) return;
      let level = 0;
      if (this.sources.size > 0 && this.analyser) {
        this.analyser.getByteTimeDomainData(data);
        let sum = 0;
        for (let i = 0; i < data.length; i++) { const v = (data[i] - 128) / 128; sum += v * v; }
        level = Math.min(1, Math.sqrt(sum / data.length) * 3.2);
      } else if (this.micOn) {
        level = Math.min(1, this.micLevel * 6);
      }
      this.h.onLevel?.(level);
      this.raf = requestAnimationFrame(tick);
    };
    this.raf = requestAnimationFrame(tick);
  }
}
