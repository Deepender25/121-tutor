"use client";
// Settings sheet (voice with previews, theme, system status, shortcuts) and the top-bar status pill.
import { useEffect, useState, useSyncExternalStore } from "react";
import { api } from "@/lib/api";
import { refreshStatus, statusStore } from "@/lib/status";
import { useStore } from "@/lib/store";
import { getTheme, setTheme, type Theme } from "@/lib/theme";
import type { Status } from "@/lib/types";
import { Icon } from "./Icon";
import { sheet, toast } from "./overlays";

const VOICE_NOTES: Record<string, string> = {
  Kore: "Firm and clear", Puck: "Upbeat and lively", Charon: "Calm and informative", Zephyr: "Bright and friendly",
  Fenrir: "Energetic", Leda: "Youthful and light", Orus: "Deep and steady", Aoede: "Warm and breezy",
};
const DEFAULT_VOICES = ["Kore", "Puck", "Charon", "Zephyr", "Fenrir", "Leda", "Orus", "Aoede"];

// ---------------------------------------------------------------- voice previews
// One player shared by all voice cards: only one sample plays at a time.

const preview = { audio: null as HTMLAudioElement | null, voice: null as string | null, loading: null as string | null, version: 0 };
const previewListeners = new Set<() => void>();
const notifyPreview = () => { preview.version++; previewListeners.forEach((fn) => fn()); };
const subscribePreview = (fn: () => void) => { previewListeners.add(fn); return () => previewListeners.delete(fn); };

function stopPreview() {
  if (preview.audio) { preview.audio.pause(); preview.audio = null; }
  preview.voice = null;
  preview.loading = null;
  notifyPreview();
}

async function playPreview(voice: string) {
  if (preview.voice === voice) { stopPreview(); return; }
  stopPreview();
  preview.voice = voice;
  preview.loading = voice;
  notifyPreview();
  const audio = new Audio(`/api/voices/${voice}/preview`);
  preview.audio = audio;
  audio.addEventListener("ended", () => { if (preview.audio === audio) stopPreview(); });
  audio.addEventListener("error", () => {
    if (preview.audio !== audio) return;
    stopPreview();
    toast("Couldn't play that preview. Check the Gemini key and try again.", "error");
  });
  try {
    await audio.play();
  } catch {
    if (preview.audio === audio) stopPreview();
  } finally {
    if (preview.loading === voice) preview.loading = null;
    notifyPreview();
  }
}

// ---------------------------------------------------------------- sheet

let settingsOpen = false;

export async function openSettings() {
  if (settingsOpen) return;
  settingsOpen = true;
  // Open immediately with what we know; refresh in the background (never block on the server).
  refreshStatus();
  try {
    await sheet((close) => <SettingsSheet close={close} />);
  } finally {
    stopPreview();
    settingsOpen = false;
  }
}

function SettingsSheet({ close }: { close: (v?: unknown) => void }) {
  const s = useStore(statusStore, (st) => st.status);
  const [chosen, setChosen] = useState<string>(s?.voice || "Kore");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  useSyncExternalStore(subscribePreview, () => preview.version, () => 0);

  // When fresh status arrives, follow the saved voice unless the user already picked another one.
  useEffect(() => { if (s && !dirty) setChosen(s.voice); }, [s, dirty]);

  const choose = (v: string) => { setChosen(v); setDirty(true); };
  const voices = s?.voices || DEFAULT_VOICES;

  const save = async () => {
    setSaving(true);
    try {
      await api.saveSettings({ voice: chosen });
      await refreshStatus();
      toast(`Voice set to ${chosen}`, "success");
      window.dispatchEvent(new CustomEvent("vt-settings", { detail: { voice: chosen } }));
      close(true);
    } catch (e) {
      toast((e as Error).message, "error", 6000);
      setSaving(false);
    }
  };

  return (
    <>
      <div className="sheet-head">
        <div><h2>Settings</h2><p>Everything runs on this computer except the voice model.</p></div>
        <button className="icon-btn" aria-label="Close settings" onClick={() => close()}><Icon name="x" /></button>
      </div>
      <div className="sheet-body scroll">
        {!s ? (
          <div className="banner error"><Icon name="alert" /><div>Can&apos;t reach the server right now. Is the “Voice Tutor - API” window open?</div></div>
        ) : null}
        {s && !s.gemini_key ? (
          <div className="banner"><Icon name="key" />
            <div><b>Gemini API key missing. </b>Get a free key at aistudio.google.com/apikey, paste it into <code>.env</code> after <code>GEMINI_API_KEY=</code> and restart run.bat.</div>
          </div>
        ) : null}

        <Section title="Voice" hint="Press play to hear a voice. The first preview of each takes a few seconds.">
          <div className="voice-grid" role="radiogroup" aria-label="Voice">
            {voices.map((v) => <VoiceCard key={v} voice={v} selected={v === chosen} onChoose={() => choose(v)} />)}
          </div>
        </Section>

        <Section title="Appearance"><ThemeSwitch /></Section>

        <Section title="System"><StatusList s={s} /></Section>

        <Section title="Keyboard shortcuts">
          <div className="shortcuts">
            <Shortcut keys={["/"]} label="Type a message" />
            <Shortcut keys={["M"]} label="Mute or unmute the mic during a conversation" />
            <Shortcut keys={["Esc"]} label="End the conversation, or close a dialog" />
            <Shortcut keys={["←", "→"]} label="Previous / next page in the viewer" />
            <Shortcut keys={["F"]} label="Full-screen reading with the floating tutor" />
            <Shortcut keys={["Ctrl", "Scroll"]} label="Zoom the page" />
          </div>
        </Section>
      </div>
      <div className="sheet-foot">
        <span className="sheet-note">Voice changes apply the next time you start talking.</span>
        <div className="spacer" />
        <button className="btn" onClick={() => close()}>Cancel</button>
        <button className="btn primary" disabled={saving || !(s && chosen !== s.voice)} onClick={save}>{saving ? "Saving…" : "Save changes"}</button>
      </div>
    </>
  );
}

function VoiceCard({ voice, selected, onChoose }: { voice: string; selected: boolean; onChoose: () => void }) {
  const loading = preview.loading === voice;
  const playing = preview.voice === voice && !loading;
  return (
    <div className={`voice-opt${selected ? " selected" : ""}`} role="radio" tabIndex={0} aria-checked={selected}
      onClick={onChoose}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onChoose(); } }}>
      <button className={`voice-play${playing ? " playing" : ""}`} aria-label={playing ? `Stop ${voice} preview` : `Preview ${voice}`}
        onClick={(e) => { e.stopPropagation(); playPreview(voice); }}>
        <Icon name={loading ? "loader" : playing ? "pause" : "play"} className="sm" />
      </button>
      <span className="vtext"><b>{voice}</b><small>{VOICE_NOTES[voice] || ""}</small></span>
      <span className="vcheck"><Icon name="check" className="sm" /></span>
    </div>
  );
}

function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section className="sheet-section">
      <div className="sheet-section-head"><h3>{title}</h3>{hint ? <p>{hint}</p> : null}</div>
      {children}
    </section>
  );
}

function StatusRow({ ic, label, value, state }: { ic: string; label: string; value: string; state: string }) {
  return (
    <div className={`status-row ${state}`}>
      <span className="status-ic"><Icon name={ic} className="sm" /></span>
      <span className="status-label">{label}</span>
      <span className="status-value" title={value}>{value}</span>
      <span className="status-dot" />
    </div>
  );
}

function StatusList({ s }: { s: Status | null }) {
  return (
    <div className="status-list">
      <StatusRow ic="wave" label="Voice model" value={s?.live_model || "—"} state={s ? "ok" : "bad"} />
      <StatusRow ic="key" label="Gemini key" value={s?.gemini_key ? "Configured" : "Missing"} state={s?.gemini_key ? "ok" : "warn"} />
      <StatusRow ic="globe" label="Web search"
        value={s?.web_provider === "tavily" ? "Tavily" : "DuckDuckGo · add TAVILY_API_KEY for better results"}
        state={s?.web_provider === "tavily" ? "ok" : "neutral"} />
      <StatusRow ic="cpu" label="Embeddings" value={s?.embedder === "ready" ? "Ready · runs locally" : (s?.embedder || "—")}
        state={s?.embedder === "ready" ? "ok" : s?.embedder === "error" ? "bad" : "warn"} />
      {s?.log_file ? <StatusRow ic="logs" label="Logs" value={s.log_file} state="neutral" /> : null}
    </div>
  );
}

function Shortcut({ keys, label }: { keys: string[]; label: string }) {
  return (
    <div className="shortcut">
      <span>{label}</span>
      <span className="keys">{keys.map((k) => <kbd key={k}>{k}</kbd>)}</span>
    </div>
  );
}

function ThemeSwitch() {
  const [current, setCurrent] = useState<Theme>(() => getTheme());
  const options: [Theme, string, string][] = [["system", "monitor", "System"], ["light", "sun", "Light"], ["dark", "moon", "Dark"]];
  return (
    <div className="seg" role="radiogroup" aria-label="Theme">
      {options.map(([value, ic, label]) => (
        <button key={value} className={value === current ? "on" : ""} role="radio" aria-checked={value === current}
          onClick={() => { setTheme(value); setCurrent(value); }}>
          <Icon name={ic} className="sm" />{label}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- status pill
// Shown only when something needs attention; otherwise nothing (keeps the top bar quiet).

export function StatusPill() {
  const s = useStore(statusStore, (st) => st.status, null);
  const checked = useStore(statusStore, (st) => st.checked, false);
  if (!checked) return null;
  let cls = "", text = "";
  if (!s) { cls = "bad"; text = "Server offline"; }
  else if (!s.gemini_key) { cls = "warn"; text = "Add Gemini key"; }
  else if (s.embedder === "loading") { cls = "warn"; text = "Warming up"; }
  else if (s.embedder === "error") { cls = "bad"; text = "Model error"; }
  if (!text) return null;
  return (
    <button className={`pill ${cls} status-pill`} title="Open settings" onClick={() => openSettings()}>
      <span className="dot" />{text}
    </button>
  );
}
