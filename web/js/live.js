// LiveClient: one voice conversation with the server's Gemini Live bridge.
//   mic (any rate) -> worklet -> 16 kHz PCM16 -> WebSocket -> server -> Gemini
//   Gemini -> server -> 24 kHz PCM16 -> scheduled AudioBuffers -> speakers
// Events from the server (transcripts, tool activity, status) are passed to `onMessage`.

const OUTPUT_RATE = 24000;

export class LiveClient {
  constructor(notebookId, { onMessage, onState, onLevel, onError }) {
    this.notebookId = notebookId;
    this.onMessage = onMessage;
    this.onState = onState;
    this.onLevel = onLevel;
    this.onError = onError;
    this.ws = null;
    this.ctx = null;
    this.state = "idle"; // idle | connecting | ready | reconnecting | closed
    this.micOn = false;
    this.speakerMuted = false;
    this.micLevel = 0;
    this.sources = new Set();
    this.nextTime = 0;
    this.pendingText = [];
    this._raf = null;
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
    this._setState("connecting");
    try {
      this.ctx = new (window.AudioContext || window.webkitAudioContext)();
      if (this.ctx.state === "suspended") await this.ctx.resume();
      this.gain = this.ctx.createGain();
      this.gain.gain.value = this.speakerMuted ? 0 : 1;
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 512;
      this.gain.connect(this.analyser);
      this.analyser.connect(this.ctx.destination);
      this._levelLoop();
    } catch (e) {
      this._fail("Your browser couldn't start audio. Try Chrome or Edge.");
      return;
    }
    if (mic) {
      const ok = await this.enableMic();
      if (!ok) { this.end(); return; }
    }
    this._openSocket();
  }

  _openSocket() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/api/notebooks/${this.notebookId}/live`);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => {
      for (const text of this.pendingText.splice(0)) ws.send(JSON.stringify({ type: "text", text }));
    };
    ws.onmessage = (e) => {
      if (typeof e.data !== "string") { this._play(e.data); return; }
      let msg;
      try { msg = JSON.parse(e.data); } catch { return; }
      if (msg.type === "status") {
        if (msg.state === "ready") this._setState("ready");
        else if (msg.state === "reconnecting" || msg.state === "connecting") this._setState(msg.state);
        else if (msg.state === "idle") this.onMessage?.({ type: "notice", text: "Voice session paused after a few quiet minutes. Tap the orb to continue." });
      } else if (msg.type === "interrupted") {
        this.flushAudio();
      } else if (msg.type === "error") {
        this.onError?.(msg.message, !!msg.fatal);
      }
      this.onMessage?.(msg);
    };
    ws.onclose = () => {
      if (this.ws === ws) this._teardown();
    };
    ws.onerror = () => {
      if (this.state === "connecting") this.onError?.("Couldn't connect to the voice server.", true);
    };
  }

  end() {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try { this.ws.send(JSON.stringify({ type: "close" })); } catch { /* closing anyway */ }
    }
    const ws = this.ws;
    this.ws = null;
    try { ws?.close(); } catch { /* ignore */ }
    this._teardown();
  }

  _teardown() {
    this.disableMic(true);
    this.flushAudio();
    cancelAnimationFrame(this._raf);
    this._raf = null;
    if (this.ctx) { this.ctx.close().catch(() => {}); this.ctx = null; }
    this.ws = null;
    this.pendingText = [];
    this._setState("idle");
    this.onLevel?.(0);
  }

  _setState(state) {
    if (this.state === state) return;
    this.state = state;
    this.onState?.(state);
  }

  _fail(message) {
    this.onError?.(message, true);
    this._setState("idle");
  }

  // ---- microphone -------------------------------------------------------------------

  async enableMic() {
    if (this.micOn) return true;
    if (!navigator.mediaDevices?.getUserMedia) {
      this.onError?.("Microphone isn't available here. Open the app at http://127.0.0.1 in Chrome or Edge.", false);
      return false;
    }
    try {
      if (!this.stream) {
        this.stream = await navigator.mediaDevices.getUserMedia({
          audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
        await this.ctx.audioWorklet.addModule("/js/audio/recorder-worklet.js");
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
      this.recorder.port.postMessage({ type: "enable", value: true });
      this.micOn = true;
      this.onState?.(this.state);
      return true;
    } catch (e) {
      const blocked = e && (e.name === "NotAllowedError" || e.name === "SecurityError");
      this.onError?.(blocked
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
    this.onState?.(this.state);
  }

  async toggleMic() {
    if (this.micOn) this.disableMic();
    else await this.enableMic();
  }

  // ---- text -------------------------------------------------------------------------

  async sendText(text) {
    if (!this.active) await this.start({ mic: false });
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: "text", text }));
    else this.pendingText.push(text);
  }

  // ---- playback ---------------------------------------------------------------------

  setSpeakerMuted(muted) {
    this.speakerMuted = muted;
    if (this.gain) this.gain.gain.value = muted ? 0 : 1;
  }

  _play(arrayBuffer) {
    if (!this.ctx) return;
    const pcm = new Int16Array(arrayBuffer);
    if (!pcm.length) return;
    const buffer = this.ctx.createBuffer(1, pcm.length, OUTPUT_RATE);
    const data = buffer.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) data[i] = pcm[i] / 0x8000;
    const src = this.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(this.gain);
    const now = this.ctx.currentTime;
    // Small jitter buffer when starting from silence; back-to-back otherwise (gapless).
    if (this.nextTime < now) this.nextTime = now + 0.06;
    src.start(this.nextTime);
    this.nextTime += buffer.duration;
    this.sources.add(src);
    const wasSpeaking = this.sources.size === 1;
    src.onended = () => {
      this.sources.delete(src);
      if (this.sources.size === 0) this.onState?.(this.state);
    };
    if (wasSpeaking) this.onState?.(this.state);
  }

  flushAudio() {
    for (const src of this.sources) {
      try { src.onended = null; src.stop(); } catch { /* already stopped */ }
    }
    this.sources.clear();
    this.nextTime = 0;
    this.onState?.(this.state);
  }

  _levelLoop() {
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
      this.onLevel?.(level);
      this._raf = requestAnimationFrame(tick);
    };
    this._raf = requestAnimationFrame(tick);
  }
}
