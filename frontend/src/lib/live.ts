// LiveClient: one voice conversation with the server's Gemini Live bridge.
//   mic (any rate) -> worklet -> 16 kHz PCM16 -> WebSocket -> server -> Gemini
//   Gemini -> server -> 24 kHz PCM16 -> scheduled AudioBuffers -> speakers
// Events from the server (transcripts, tool activity, status) are passed to `onMessage`.
// The WebSocket goes to this origin's /api, which Next.js proxies to the Python server.
import { apiWsUrl } from "./api";
import type { ToolUI } from "./types";

const OUTPUT_RATE = 24000;
const WORKLET_URL = "/audio/recorder-worklet.js";

// Android Chrome puts the whole phone into call mode (MODE_IN_COMMUNICATION) when a microphone is opened with any
// audio processing (echo cancellation, noise suppression, auto gain), and then plays every sound the page makes
// as voice-call audio, which is where the beeps and siren-like sounds came from. With all processing off the phone
// stays in media mode (Chromium media/audio/android/audio_manager_android.cc), so on Android the microphone opens
// raw and EchoGate below keeps the tutor's own voice, picked up by the microphone, from reaching Gemini.
const ANDROID = typeof navigator !== "undefined" && /Android/i.test(navigator.userAgent);
const MIC_CONSTRAINTS: MediaTrackConstraints = ANDROID
  ? { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false }
  : { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true };

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
  private inReply = false; // reply audio is flowing (a gap now is audio arriving late, not a pause)
  private diag = new PlaybackDiagnostics();
  private gate: EchoGate | null = ANDROID ? new EchoGate() : null;

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
      this.diag = new PlaybackDiagnostics();
      this.diag.start(this.ctx);
      if (this.gate) { this.gate = new EchoGate(); this.diag.extra = () => this.gate?.stats() || {}; }
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
      this.diag.onReport = (stats) => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "client_stats", ...stats })); };
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
      } else if (msg.type === "turn_complete" || msg.type === "tool_call") {
        this.inReply = false; // the next audio starts a new stretch: a pause before it is expected
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
    this.diag.report(true);
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
    this.diag.stop();
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
        this.stream = await navigator.mediaDevices.getUserMedia({ audio: MIC_CONSTRAINTS });
        await this.ctx.audioWorklet.addModule(WORKLET_URL);
        this.micSource = this.ctx.createMediaStreamSource(this.stream);
        this.recorder = new AudioWorkletNode(this.ctx, "recorder-processor");
        this.recorder.port.onmessage = (e) => {
          if (e.data.type === "level") this.micLevel = e.data.value;
          else if (e.data.type === "audio" && this.micOn && this.ws?.readyState === WebSocket.OPEN) {
            if (!this.gate || !this.ctx) { this.ws.send(e.data.buffer); return; }
            for (const frame of this.gate.filter(new Int16Array(e.data.buffer), this.ctx.currentTime)) this.ws.send(frame);
          }
        };
        this.diag.mic(this.stream);
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
    if (this.nextTime < now) {
      if (this.inReply && this.nextTime > 0) this.diag.gap((now + 0.06 - this.nextTime) * 1000);
      this.nextTime = now + 0.06;
    }
    this.inReply = true;
    this.diag.chunk(pcm.length / OUTPUT_RATE);
    src.start(this.nextTime);
    this.gate?.played(data, this.nextTime, OUTPUT_RATE);
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
    this.inReply = false;
    this.gate?.stopped();
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

// Playback diagnostics, sent to the server's log every 30 s and at the end of a session. Reply audio that arrives
// after the previous chunk finished leaves a gap: the speech stops and restarts mid-waveform, which clicks.
// Long main-thread tasks (the page busy elsewhere) are the usual cause of late handling; the context's state
// changes and the microphone's actual settings show what the phone's audio system did.
type Stats = Record<string, unknown>;

class PlaybackDiagnostics {
  onReport: ((stats: Stats) => void) | null = null;
  extra: (() => Stats) | null = null;
  private ctx: AudioContext | null = null;
  private t0 = 0;
  private audioS = 0;
  private chunks = 0;
  private gaps: string[] = []; // "at s:ms"
  private gapCount = 0;
  private gapMs = 0;
  private longTasks = 0;
  private longestTask = 0;
  private longAt: string[] = [];
  private states: string[] = [];
  private micInfo: Stats = {};
  private observer: PerformanceObserver | null = null;
  private timer: number | null = null;
  private lastSent = "";

  private at() { return Math.round((performance.now() - this.t0) / 100) / 10; }

  start(ctx: AudioContext) {
    this.ctx = ctx;
    this.t0 = performance.now();
    this.states.push(`0:${ctx.state}`);
    ctx.onstatechange = () => this.states.push(`${this.at()}:${ctx.state}`);
    try {
      this.observer = new PerformanceObserver((list) => {
        for (const e of list.getEntries()) {
          this.longTasks++;
          this.longestTask = Math.max(this.longestTask, Math.round(e.duration));
          if (e.duration >= 150 && this.longAt.length < 30) this.longAt.push(`${Math.round((e.startTime - this.t0) / 100) / 10}:${Math.round(e.duration)}`);
        }
      });
      this.observer.observe({ type: "longtask", buffered: false });
    } catch { /* not supported in this browser */ }
    this.timer = window.setInterval(() => this.report(false), 30000);
  }

  mic(stream: MediaStream) {
    const track = stream.getAudioTracks()[0];
    const set = (track?.getSettings?.() || {}) as MediaTrackSettings & Stats;
    this.micInfo = {
      label: track?.label || "", rate: set.sampleRate, ec: set.echoCancellation, ns: set.noiseSuppression, agc: set.autoGainControl,
    };
  }

  chunk(seconds: number) { this.chunks++; this.audioS += seconds; }

  gap(ms: number) {
    this.gapCount++;
    this.gapMs += ms;
    if (this.gaps.length < 40) this.gaps.push(`${this.at()}:${Math.round(ms)}`);
  }

  report(final: boolean) {
    if (!this.ctx || !this.onReport) return;
    const ctx = this.ctx as AudioContext & { outputLatency?: number };
    const stats: Stats = {
      final, t: this.at(), rate: ctx.sampleRate, baseLatencyMs: Math.round((ctx.baseLatency || 0) * 1000),
      outputLatencyMs: Math.round((ctx.outputLatency || 0) * 1000), audioS: Math.round(this.audioS), chunks: this.chunks,
      gaps: this.gapCount, gapMs: Math.round(this.gapMs), gapList: this.gaps, longTasks: this.longTasks, longestTaskMs: this.longestTask,
      longList: this.longAt, states: this.states, mic: this.micInfo, ua: navigator.userAgent, ...(this.extra?.() || {}),
    };
    const key = JSON.stringify({ ...stats, t: 0, final: false });
    if (!final && key === this.lastSent) return;
    this.lastSent = key;
    try { this.onReport(stats); } catch { /* socket closing */ }
  }

  stop() {
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null;
    this.observer?.disconnect();
    this.observer = null;
    if (this.ctx) this.ctx.onstatechange = null;
    this.ctx = null;
    this.onReport = null;
  }
}

// Echo gate (Android, where the microphone runs without the phone's echo cancellation; see MIC_CONSTRAINTS).
// While the tutor is audible, a microphone frame is mostly the tutor's own voice coming back from the speaker,
// at a level proportional to what was played a moment earlier. The gate tracks the loudest that echo gets
// relative to the playback (the coupling, learned from frames that are only echo) and sends silence in place of
// echo. Sound clearly louder than the loudest expected echo for 160 ms is you interrupting: those held frames and
// everything after go through until you stop for 0.6 s. With earphones the coupling falls to almost nothing,
// so any speech gets through.
class EchoGate {
  private env: { t: number; rms: number }[] = []; // playback loudness per 20 ms, at its scheduled time
  private coupling = 1;
  private held: ArrayBuffer[] = [];
  private talking = false; // you're talking over the tutor
  private quiet = 0;
  private gated = 0;
  private bargeIns = 0;

  played(data: Float32Array, start: number, rate: number) {
    const step = Math.round(rate * 0.02);
    for (let i = 0; i < data.length; i += step) {
      let sum = 0;
      const end = Math.min(data.length, i + step);
      for (let j = i; j < end; j++) sum += data[j] * data[j];
      this.env.push({ t: start + i / rate, rms: Math.sqrt(sum / (end - i)) });
    }
  }

  stopped() { this.env = []; }

  filter(frame: Int16Array, now: number): ArrayBuffer[] {
    while (this.env.length && this.env[0].t < now - 1) this.env.shift();
    // The echo in this frame left the speaker up to ~0.4 s ago (output and input latency plus the frame itself).
    let ref = 0;
    for (const e of this.env) if (e.t >= now - 0.4 && e.t <= now) ref = Math.max(ref, e.rms);
    let sum = 0;
    for (let i = 0; i < frame.length; i++) { const v = frame[i] / 32768; sum += v * v; }
    const rms = Math.sqrt(sum / frame.length);
    if (ref < 0.004) { // the tutor isn't audible: the microphone is all yours
      this.talking = false;
      return this.release(frame);
    }
    const loud = rms > Math.max(2.5 * this.coupling * ref, 0.015);
    if (this.talking) {
      this.quiet = loud ? 0 : this.quiet + 1;
      if (this.quiet < 15) return [frame.buffer as ArrayBuffer];
      this.talking = false;
    }
    if (loud) {
      this.held.push(frame.buffer as ArrayBuffer);
      if (this.held.length < 4) return [];
      this.talking = true;
      this.quiet = 0;
      this.bargeIns++;
      return this.release(null);
    }
    const ratio = rms / ref; // echo only: follow its peaks up quickly, let them fade slowly
    this.coupling = Math.min(4, Math.max(0.02, this.coupling + (ratio > this.coupling ? 0.2 : 0.03) * (ratio - this.coupling)));
    const silent = this.held.length + 1;
    this.held = [];
    this.gated += silent;
    return Array.from({ length: silent }, () => new ArrayBuffer(frame.byteLength));
  }

  private release(frame: Int16Array | null): ArrayBuffer[] {
    const out = this.held;
    this.held = [];
    if (frame) out.push(frame.buffer as ArrayBuffer);
    return out;
  }

  stats(): Stats {
    return { echoGate: { gatedFrames: this.gated, bargeIns: this.bargeIns, coupling: Math.round(this.coupling * 100) / 100 } };
  }
}
