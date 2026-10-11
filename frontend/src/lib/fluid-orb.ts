// FluidOrb: WebGL fluid sphere (vanilla port of the FluidOrb React component; shader unchanged).
// Reacts to the voice: the fluid flows faster and the orb swells with the audio level while the tutor
// speaks (or while you talk). Colour comes from the theme (--orb-color), so it stays monochrome.
// Stops itself when removed from the DOM; static when reduced motion is on.

const VERT = `
attribute vec2 a_pos;
void main() {
  gl_Position = vec4(a_pos, 0.0, 1.0);
}
`;

const FRAG = `
#ifdef GL_FRAGMENT_PRECISION_HIGH
precision highp float;
#else
precision mediump float;
#endif

uniform vec2 u_resolution;
uniform float u_time;
uniform vec3 u_color;

float hash(vec2 p) {
  return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}

float noise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(
    mix(hash(i + vec2(0.0, 0.0)), hash(i + vec2(1.0, 0.0)), u.x),
    mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), u.x),
    u.y
  );
}

float fbm(vec2 p) {
  float v = 0.0;
  float a = 0.6;
  for (int i = 0; i < 3; i++) {
    v += a * noise(p);
    p *= 2.0;
    a *= 0.5;
  }
  return v;
}

void main() {
  vec2 uv = gl_FragCoord.xy / u_resolution.xy;
  float t = u_time * 0.22;

  vec2 drift = vec2(
    sin(t) + 0.6 * sin(t * 1.7 + 1.3),
    cos(t * 0.8) + 0.6 * cos(t * 1.3 + 2.1)
  );

  vec2 p = vec2(uv.x * 1.8, uv.y * 1.0) + drift * 0.7;

  vec2 q = vec2(fbm(p + drift), fbm(p + vec2(3.2, 1.5) - drift));
  float f = fbm(p + 1.2 * q);

  float g = clamp(1.0 - uv.y, 0.0, 1.0);
  float anchor = smoothstep(0.0, 0.3, uv.y);
  float shade = clamp(g + (f - 0.5) * 0.8 * anchor, 0.0, 1.0);

  vec3 white = vec3(0.99, 1.0, 1.0);
  vec3 light = mix(white, u_color, 0.5);
  vec3 dark = u_color;

  vec3 col = white;
  col = mix(col, light, smoothstep(0.28, 0.52, shade));
  col = mix(col, dark, smoothstep(0.58, 0.88, shade));

  float edge = smoothstep(0.5, 0.49, distance(uv, vec2(0.5)));

  gl_FragColor = vec4(col * edge, edge);
}
`;

export function hexToRgb(hex: string): [number, number, number] {
  let h = String(hex).replace("#", "").trim();
  if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
  const n = parseInt(h, 16);
  if (h.length !== 6 || Number.isNaN(n)) return [0.16, 0.16, 0.18];
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
}

// The orb colour for the current theme (CSS custom properties --orb-color / --orb-muted).
export function themeOrbColor(muted = false): string {
  const css = getComputedStyle(document.documentElement);
  return (css.getPropertyValue(muted ? "--orb-muted" : "--orb-color") || "#2a2a2e").trim();
}

function compile(gl: WebGLRenderingContext, type: number, src: string): WebGLShader | null {
  const shader = gl.createShader(type);
  if (!shader) return null;
  gl.shaderSource(shader, src);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    console.error(gl.getShaderInfoLog(shader));
    gl.deleteShader(shader);
    return null;
  }
  return shader;
}

export type OrbState = "idle" | "muted" | "listening" | "connecting" | "thinking" | "speaking";

// How fast the fluid flows in each state (multiplier on time), and how much the voice level adds.
const FLOW: Record<OrbState, number> = { idle: 0.8, muted: 0.35, listening: 1, connecting: 2.4, thinking: 2.8, speaking: 1.2 };
const LEVEL_FLOW: Partial<Record<OrbState, number>> = { listening: 3.5, speaking: 7 };

export class FluidOrb {
  size: number;
  state: OrbState;
  el: HTMLDivElement;
  private canvas: HTMLCanvasElement;
  private color: [number, number, number];
  private target: [number, number, number];
  private level = 0;
  private smooth = 0;
  private time = Math.random() * 100; // different orbs don't move in lockstep
  private reduce: boolean;
  private paused = false;
  private wasConnected = false;
  private last = 0;
  private raf: number | null = null;
  private gl: WebGLRenderingContext | null = null;
  private glObjects: { program: WebGLProgram; vert: WebGLShader; frag: WebGLShader; buffer: WebGLBuffer | null } | null = null;
  private uTime: WebGLUniformLocation | null = null;
  private uColor: WebGLUniformLocation | null = null;
  private onTheme: () => void;
  private mq: MediaQueryList;

  constructor({ size = 240, className = "", state = "idle" as OrbState } = {}) {
    this.size = size;
    this.state = state;
    this.color = hexToRgb(themeOrbColor(state === "muted"));
    this.target = [...this.color];
    this.el = document.createElement("div");
    this.el.className = `fluid-orb ${className}`.trim();
    this.el.dataset.slot = "fluid-orb";
    this.el.style.width = `${size}px`;
    this.el.style.height = `${size}px`;
    this.canvas = document.createElement("canvas");
    this.el.append(this.canvas);
    this.reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    this.onTheme = () => this.refreshColor();
    window.addEventListener("vt-theme", this.onTheme);
    this.mq = window.matchMedia("(prefers-color-scheme: dark)");
    this.mq.addEventListener?.("change", this.onTheme);
    if (!this.init()) this.el.classList.add("fluid-orb-fallback");
  }

  private init(): boolean {
    const gl = this.canvas.getContext("webgl", { antialias: true, alpha: true, premultipliedAlpha: true });
    if (!gl) return false;
    const program = gl.createProgram();
    const vert = compile(gl, gl.VERTEX_SHADER, VERT);
    const frag = compile(gl, gl.FRAGMENT_SHADER, FRAG);
    if (!program || !vert || !frag) return false;
    gl.attachShader(program, vert);
    gl.attachShader(program, frag);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      console.error(gl.getProgramInfoLog(program));
      return false;
    }
    gl.useProgram(program);

    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), gl.STATIC_DRAW);
    const aPos = gl.getAttribLocation(program, "a_pos");
    gl.enableVertexAttribArray(aPos);
    gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);

    this.uTime = gl.getUniformLocation(program, "u_time");
    this.uColor = gl.getUniformLocation(program, "u_color");
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const px = Math.round(this.size * dpr);
    this.canvas.width = px;
    this.canvas.height = px;
    gl.viewport(0, 0, px, px);
    gl.uniform2f(gl.getUniformLocation(program, "u_resolution"), px, px);
    gl.uniform3f(this.uColor, ...this.color);

    this.gl = gl;
    this.glObjects = { program, vert, frag, buffer };
    this.canvas.addEventListener("webglcontextlost", (e) => { e.preventDefault(); this.destroy(); this.el.classList.add("fluid-orb-fallback"); });

    this.last = performance.now();
    this.raf = requestAnimationFrame((now) => this.render(now));
    return true;
  }

  private render(now: number) {
    if (!this.gl) return;
    if (!this.el.isConnected && this.wasConnected) { this.destroy(); return; }
    if (this.el.isConnected) this.wasConnected = true;
    const dt = Math.min(0.1, (now - this.last) / 1000);
    this.last = now;
    // Fast attack, slower release: the orb jumps with each syllable and settles smoothly.
    const k = this.level > this.smooth ? 18 : 6;
    this.smooth += (this.level - this.smooth) * Math.min(1, dt * k);
    for (let i = 0; i < 3; i++) this.color[i] += (this.target[i] - this.color[i]) * Math.min(1, dt * 4);
    if (!document.hidden && !this.paused) {
      const flow = (FLOW[this.state] ?? 1) + (LEVEL_FLOW[this.state] || 0) * this.smooth;
      if (!this.reduce) this.time += dt * flow;
      this.gl.uniform1f(this.uTime, this.time);
      this.gl.uniform3f(this.uColor, ...this.color);
      this.gl.drawArrays(this.gl.TRIANGLES, 0, 6);
      this.el.style.setProperty("--level", this.smooth.toFixed(3));
    }
    this.raf = this.reduce ? null : requestAnimationFrame((t) => this.render(t));
  }

  setState(state: OrbState) {
    this.state = state;
    this.target = hexToRgb(themeOrbColor(state === "muted"));
    if (this.reduce && this.gl) this.render(performance.now());
  }

  // Stop drawing while the orb is covered (e.g. by full-screen reading); the loop keeps its place.
  setPaused(paused: boolean) {
    this.paused = paused;
  }

  setLevel(level: number) {
    this.level = Math.max(0, Math.min(1, level || 0));
  }

  refreshColor() {
    this.target = hexToRgb(themeOrbColor(this.state === "muted"));
  }

  destroy() {
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = null;
    window.removeEventListener("vt-theme", this.onTheme);
    this.mq?.removeEventListener?.("change", this.onTheme);
    const gl = this.gl;
    if (!gl || !this.glObjects) return;
    const { program, vert, frag, buffer } = this.glObjects;
    gl.deleteProgram(program);
    gl.deleteShader(vert);
    gl.deleteShader(frag);
    gl.deleteBuffer(buffer);
    gl.getExtension("WEBGL_lose_context")?.loseContext();
    this.gl = null;
  }
}
