// Notebook controller: everything the notebook page does, outside React. The view components render from
// `store` and call these actions; long-lived engines (voice session, PDF viewer, voice orbs, follow-along
// tracker) and DOM measurements live here.
//
//   sources + outline | conversation + voice | PDF viewer, with draggable splits between them, and a
//   full-screen reading mode where the PDF fills the screen and the tutor becomes a floating orb.
import type { KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from "react";
import { api } from "@/lib/api";
import { h, iconEl } from "@/lib/dom";
import { FluidOrb, type OrbState } from "@/lib/fluid-orb";
import { FollowTracker, type Candidate, type Segment } from "@/lib/follow";
import { LiveClient, type LiveMessage } from "@/lib/live";
import { PdfView } from "@/lib/pdf-view";
import { refreshStatus, statusStore } from "@/lib/status";
import { createStore } from "@/lib/store";
import type { HistoryEntry, Job, Notebook, Outline, OutlineSection, SearchRef, ToolUI } from "@/lib/types";
import { confirmDialog, openMenu, toast } from "../overlays";
import { openSettings } from "../Settings";

// ---------------------------------------------------------------- labels

export const TOOL_LABELS: Record<string, (a: Record<string, unknown>) => [string, string]> = {
  get_outline: () => ["list", "Checking the outline"],
  read_passage: (a) => ["book", passageLabel(String(a.target || "next").trim())],
  search_sources: (a) => ["search", `Searching your sources for “${a.query || ""}”`],
  update_progress: (a) => ["listChecks", progressLabel(a)],
  web_search: (a) => ["globe", `Searching the web for “${a.query || ""}”`],
};

// Append a transcript piece, adding the space that goes missing when speech resumes after a tool call.
export function joinText(previous: string, piece: string): string {
  if (previous && piece && !/\s$/.test(previous) && !/^\s/.test(piece) && /[.!?,;:]$/.test(previous)) return `${previous} ${piece}`;
  return previous + piece;
}

function progressLabel(a: Record<string, unknown>): string {
  const target = String(a.target || "").trim();
  const all = /^(all|everything|(the )?(whole |entire )?document)$/i.test(target);
  const what = all ? "everything" : a.through ? `everything up to “${target}”` : `“${target}”`;
  return a.completed === false ? `Marking ${what} as not done` : `Marking ${what} as completed`;
}

function passageLabel(target: string): string {
  const t = target.toLowerCase();
  if (t === "next" || t === "continue") return "Reading the next part";
  if (["current", "again", "repeat"].includes(t)) return "Re-reading this part";
  if (["previous", "prev", "back"].includes(t)) return "Going back a part";
  if (["start", "beginning", "first"].includes(t)) return "Starting from the beginning";
  const page = t.match(/^page[\s:]*(\d+)$/);
  if (page) return `Opening page ${page[1]}`;
  if (/^s\d+$/.test(t)) return "Opening the section";
  return `Opening “${target}”`;
}

export function refLabel(ref: ToolUI): [string, string] {
  if (ref.kind === "search") return ["search", `Searched your sources for “${ref.query}”`];
  if (ref.kind === "web") return ["globe", `Searched the web for “${ref.query}”`];
  if (ref.kind === "passage") return ["book", "Read"];
  if (ref.kind === "progress") return ["listChecks", `${ref.summary} · ${ref.covered} covered`];
  return ["sparkles", ""];
}

export const QUICK: [string, string, string, string][] = [
  ["start", "play", "Start lesson", "Teach me from the beginning."],
  ["continue", "play", "Continue", "Continue where we left off."],
  ["quiz", "messageQuestion", "Quiz me", "Quiz me on what we just covered, one question at a time."],
  ["summary", "listChecks", "Summarize", "Summarize the current section, covering every key point."],
  ["web", "globe", "What's new?", "Search the web for anything newer or different from what my sources say."],
];

export function jobStageText(j: Job): string {
  if (j.stage === "queued") return "Waiting…";
  if (j.kind === "reindex") {
    // switched embedding model: the stored text gets new vectors; keyword search works meanwhile
    return j.stage === "embedding" ? `Updating search · ${j.done} of ${j.total}` : j.stage === "saving" ? "Saving…" : "Updating search…";
  }
  if (j.stage === "parsing") return "Reading the PDF…";
  if (j.stage === "chunking") return "Finding sections…";
  if (j.stage === "embedding") return `Indexing ${j.done} of ${j.total}`;
  if (j.stage === "saving") return "Saving…";
  return "Done";
}

// ---------------------------------------------------------------- state

export interface Act {
  key: string;
  icon: string;
  label: string;
  running: boolean;
  error: string | null;
  ui: ToolUI | null;
}

export interface Msg {
  id: number;
  role: "user" | "model";
  text: string;
  live: boolean;
  ts: string | null;
  interrupted: boolean;
  acts: Act[];
}

export type Tab = "sources" | "chat" | "page";
// While talking: "transcript" keeps the conversation on screen with a floating orb above the text bar;
// "orb" is the focused full view with the big orb. Tapping the orb switches between them.
export type VoiceView = "transcript" | "orb";

export interface NbState {
  nb: Omit<Notebook, "history" | "jobs">;
  jobs: Job[];
  outline: Outline | null;
  outlineError: string | null;
  messages: Msg[];
  voice: { key: OrbState; label: string; sub: string; aria: string; icon: string; active: boolean; micOn: boolean };
  view: VoiceView;
  stage: { show: boolean; liveOrb: boolean; label: string; activity: string; caption: string; captionUser: boolean; compact: boolean; tiny: boolean };
  viewer: { docId: string | null; title: string | null; page: number | null; total: number | null; zoom: number };
  follow: boolean;
  pill: string | null;
  reading: boolean;
  idle: boolean;
  bubble: { show: boolean; who: "model" | "user" | "hint"; text: string; busy: boolean; actIcon: string; actLabel: string };
  composerOpen: boolean;
  layout: { viewerHidden: boolean; viewerOpen: boolean; tab: Tab; collapsed: boolean; resizing: boolean; dragging: "sources" | "viewer" | null; toggleActive: boolean };
  drop: boolean;
}

type ElName = "nb" | "body" | "messages" | "voiceStage" | "stageOrb" | "stage" | "orbBtn" | "liveOrbBtn" | "floatOrbBtn" | "textarea"
  | "floatInput" | "viewerPanel" | "fileInput" | "gutterSources" | "gutterViewer" | "floatDock";

const VIEWPORT = { phone: "(max-width: 760px)", narrow: "(max-width: 980px)", wide: "(min-width: 981px)" };
const matches = (q: string) => window.matchMedia(q).matches;

// Resizable splits. Sources width is kept in px; the viewer's as a share of the width, so it scales with
// the window. The conversation always keeps at least CHAT_MIN. Dragging the sources edge far left collapses it.
const SPLIT_KEY = "vt-split";
const SOURCES = { min: 220, max: 440, def: 280, collapseBelow: 150 };
const VIEWER = { min: 320, def: 0.46 };
const CHAT_MIN = 340;
const GUTTER = 12;

let msgSeq = 0;

function historyMessages(history: HistoryEntry[]): Msg[] {
  return history
    .filter((e) => e.role === "user" || e.role === "model")
    .map((e) => ({
      id: ++msgSeq,
      role: e.role,
      text: e.text,
      live: false,
      ts: e.ts || null,
      interrupted: false,
      acts: e.role === "model"
        ? (e.refs || []).map((ref, i) => { const [icon, label] = refLabel(ref); return { key: `h${i}`, icon, label, running: false, error: null, ui: ref }; })
        : [],
    }));
}

// ---------------------------------------------------------------- controller

export type NotebookController = ReturnType<typeof createNotebookController>;

export function createNotebookController(initial: Notebook) {
  const id = initial.id;
  let nb: Notebook = initial;

  const store = createStore<NbState>({
    nb: { ...initial },
    jobs: [...(initial.jobs || [])],
    outline: null,
    outlineError: null,
    messages: historyMessages(initial.history || []),
    voice: { key: "idle", label: "Tap to talk", sub: "or type a message", aria: "Start talking", icon: "mic", active: false, micOn: false },
    view: "transcript",
    stage: { show: false, liveOrb: false, label: "", activity: "", caption: "", captionUser: false, compact: false, tiny: false },
    viewer: { docId: null, title: null, page: null, total: null, zoom: 1 },
    follow: true,
    pill: null,
    reading: false,
    idle: false,
    bubble: { show: false, who: "model", text: "", busy: false, actIcon: "sparkles", actLabel: "" },
    composerOpen: false,
    layout: { viewerHidden: false, viewerOpen: false, tab: "chat", collapsed: false, resizing: false, dragging: null, toggleActive: true },
    drop: false,
  });
  const get = () => store.get();
  const patch = <K extends keyof NbState>(key: K, value: Partial<NbState[K]>) =>
    store.set((s) => ({ [key]: { ...(s[key] as object), ...value } } as unknown as Partial<NbState>));

  // ---- elements (registered by the view)
  const els: Partial<Record<ElName, HTMLElement>> = {};
  const refFns: Partial<Record<ElName, (el: HTMLElement | null) => void>> = {};
  function ref(name: ElName) {
    return (refFns[name] ||= (el: HTMLElement | null) => { if (el) els[name] = el; else delete els[name]; });
  }

  // ---- mutable session state (not rendered directly)
  const S = {
    viewerDocId: null as string | null,
    follow: true,
    followPaused: false, // you scrolled or jumped away while following; resumes on your next question
    focus: null as { doc_id: string; page: number; rects: number[][] } | null, // the spot the tutor is talking about
    turnHasTools: false,
    reading: false,
    toolsPending: new Map<string, string>(),
    user: null as { id: number; text: string } | null, // live user bubble
    model: null as { id: number; text: string; acts: Map<string, string> } | null, // live model message
    disposed: true,
    voiceKey: "idle" as OrbState,
    view: "transcript" as VoiceView,
    wantScroll: false,
  };
  const timers = new Set<ReturnType<typeof setTimeout>>();
  const later = (fn: () => void, ms: number) => { const t = setTimeout(() => { timers.delete(t); fn(); }, ms); timers.add(t); return t; };

  let live: LiveClient | null = null;
  let pdf: PdfView | null = null;
  let dockOrb: FluidOrb | null = null;
  let stageOrb: FluidOrb | null = null;
  let liveOrb: FluidOrb | null = null; // floating above the text bar in transcript mode
  let floatOrb: FluidOrb | null = null;
  const tracker = new FollowTracker();
  const cleanups: (() => void)[] = [];

  // ================================================================ messages

  function setMessages(fn: (m: Msg[]) => Msg[]) { store.set((s) => ({ messages: fn(s.messages) })); }
  function patchMsg(mid: number, fn: (m: Msg) => Msg) { setMessages((list) => list.map((m) => (m.id === mid ? fn(m) : m))); }

  function addUser(text: string, isLive = false, ts: string | null = null) {
    const mid = ++msgSeq;
    setMessages((list) => [...list, { id: mid, role: "user", text, live: isLive, ts, interrupted: false, acts: [] }]);
    return mid;
  }

  function ensureModel() {
    if (!S.model) {
      const mid = ++msgSeq;
      setMessages((list) => [...list, { id: mid, role: "model", text: "", live: true, ts: new Date().toISOString(), interrupted: false, acts: [] }]);
      S.model = { id: mid, text: "", acts: new Map() };
    }
    return S.model;
  }

  function finalizeUser() {
    if (!S.user) return;
    const { id: mid, text } = S.user;
    if (!text.trim()) setMessages((list) => list.filter((m) => m.id !== mid));
    else patchMsg(mid, (m) => ({ ...m, live: false }));
    S.user = null;
  }

  function finalizeModel() {
    if (!S.model) return;
    const mid = S.model.id;
    setMessages((list) => list.flatMap((m) => {
      if (m.id !== mid) return [m];
      if (!m.text.trim() && !m.acts.length) return [];
      return [{ ...m, live: false, acts: m.acts.map((a) => (a.running ? { ...a, running: false } : a)) }];
    }));
    S.model = null;
  }

  function finalizeTurn() {
    finalizeUser();
    finalizeModel();
    S.toolsPending.clear();
    paintVoice();
  }

  // The message list sticks to the bottom while you're there; measured before the new content renders.
  function scrollToEnd(force = false) {
    const m = els.messages;
    const near = !m || m.scrollHeight - m.scrollTop - m.clientHeight < 160;
    if (near || force) S.wantScroll = true;
  }

  function flushScroll() {
    if (!S.wantScroll || !els.messages) return;
    S.wantScroll = false;
    els.messages.scrollTop = els.messages.scrollHeight;
  }

  // ================================================================ live voice

  async function ensureReady() {
    const s = statusStore.get().status || (await refreshStatus());
    if (!s) { toast("Can't reach the server. Start it with run.bat.", "error"); return false; }
    if (!s.gemini_key) { openSettings(); return false; }
    return true;
  }

  async function onOrb() {
    if (!live || !(await ensureReady())) return;
    if (!live.active) await live.start({ mic: true });
    else await live.toggleMic();
  }

  function toolHint() {
    const names = [...S.toolsPending.values()];
    if (names.includes("web_search")) return "searching the web";
    if (names.includes("search_sources")) return "searching your sources";
    if (names.includes("read_passage")) return "reading";
    return "checking";
  }

  function paintVoice() {
    if (!live) return;
    const st = live.state;
    let label = "Tap to talk", sub = "or type a message", key: OrbState = "idle", ic = "mic", aria = "Start talking";
    if (st === "connecting") { label = "Connecting…"; sub = "one moment"; key = "connecting"; }
    else if (st === "reconnecting") { label = "Reconnecting…"; sub = "keeping your place"; key = "connecting"; }
    else if (st === "ready") {
      if (S.toolsPending.size) { label = "Looking it up…"; sub = toolHint(); key = "thinking"; }
      else if (live.speaking) { label = "Speaking"; sub = live.micOn ? "just talk to interrupt" : "type or unmute to interrupt"; key = "speaking"; }
      else if (live.micOn) { label = "Listening"; sub = "go ahead"; key = "listening"; }
      else { label = "Mic off"; sub = "unmute to talk, or type"; key = "muted"; }
      ic = live.micOn ? "mic" : "micOff";
      aria = live.micOn ? "Mute microphone (M)" : "Unmute microphone (M)";
    }
    S.voiceKey = key;
    const on = live.active;
    if (!on) S.toolsPending.clear();
    store.set({ voice: { key, label, sub, aria, icon: ic, active: on, micOn: live.micOn } });
    patch("stage", { label: key === "idle" ? "" : label });
    paintStage();
    dockOrb?.setState(key);
    stageOrb?.setState(key);
    liveOrb?.setState(key);
    floatOrb?.setState(key);
    if (S.reading) paintBubbleHead();
    paintFollowPill();
  }

  function setView(view: VoiceView) {
    S.view = view;
    try { localStorage.setItem("vt-voice-view", view); } catch { /* storage blocked */ }
    store.set({ view });
    paintStage();
    if (view === "transcript") { scrollToEnd(true); requestAnimationFrame(flushScroll); }
  }

  function paintStage() {
    const active = !!live?.active;
    const show = active && S.view === "orb";
    const floating = active && S.view === "transcript" && !S.reading;
    if (floating !== get().stage.liveOrb) {
      scrollToEnd(); // the transcript gains room for the orb: stay at the latest message if you were there
      requestAnimationFrame(flushScroll);
    }
    patch("stage", { show, liveOrb: floating });
    if (show) requestAnimationFrame(fitStage); // after the stage is displayed, so it can be measured
    if (floating && !liveOrb) {
      liveOrb = new FluidOrb({ size: 68, state: S.voiceKey });
      els.liveOrbBtn?.prepend(liveOrb.el);
    } else if (!floating && liveOrb) {
      liveOrb.destroy();
      liveOrb.el.remove();
      liveOrb = null;
    }
    dockOrb?.setPaused(active || S.reading); // the bar's orb is hidden while talking
    if (!active && stageOrb) {
      stageOrb.destroy();
      stageOrb = null;
      els.stageOrb?.replaceChildren();
      patch("stage", { caption: "", activity: "" });
    }
  }

  // Size the stage orb to the space actually available (short windows get a small orb, no overlap).
  function fitStage() {
    const stage = els.voiceStage;
    if (!stage || !get().stage.show) return;
    const height = stage.clientHeight || els.messages?.clientHeight || 400;
    const width = stage.clientWidth || els.messages?.clientWidth || 400;
    patch("stage", { compact: height < 380, tiny: height < 300 }); // tiny: too short for captions (the transcript has them)
    const reserved = height < 380 ? 110 : 190; // label and caption (mic and End live in the text bar)
    const size = Math.round(Math.max(56, Math.min(180, height - reserved, width * 0.42)));
    if (stageOrb && Math.abs(stageOrb.size - size) < 8) return;
    stageOrb?.destroy();
    stageOrb = new FluidOrb({ size, className: "stage", state: S.voiceKey });
    els.stageOrb?.replaceChildren(stageOrb.el);
    stageOrb.setPaused(S.reading);
  }

  function setLevel(level: number) {
    const v = level.toFixed(3);
    els.orbBtn?.style.setProperty("--level", v);
    els.liveOrbBtn?.style.setProperty("--level", v);
    els.voiceStage?.style.setProperty("--level", v);
    els.floatOrbBtn?.style.setProperty("--level", v);
    dockOrb?.setLevel(level);
    stageOrb?.setLevel(level);
    liveOrb?.setLevel(level);
    floatOrb?.setLevel(level);
  }

  function caption(text: string, who: "user" | "model") {
    const clean = text.replace(/\s+/g, " ").trim();
    patch("stage", { caption: clean.length > 240 ? `…${clean.slice(-240)}` : clean, captionUser: who === "user" });
    bubbleSay(who, clean);
  }

  // ---- floating bubble (reading mode): what the tutor is saying, what you said, what it's looking up
  let bubbleTimer: ReturnType<typeof setTimeout> | undefined;
  let heardCaption = ""; // the reply so far, as heard
  let bubbleWho: "model" | "user" | "hint" = "model";

  function bubbleSay(who: "model" | "user" | "hint", text: string) {
    bubbleWho = who;
    clearTimeout(bubbleTimer);
    patch("bubble", { who, text: text.length > 280 ? `…${text.slice(-280).replace(/^\S*\s/, "")}` : text });
    paintBubbleHead();
    if (text) patch("bubble", { show: true });
  }

  function paintBubbleHead() {
    const activity = S.toolsPending.size ? get().stage.activity : "";
    const last = [...S.toolsPending.values()].pop();
    const actIcon = activity && last ? (TOOL_LABELS[last]?.({})[0] || "sparkles") : "sparkles";
    patch("bubble", { busy: !!activity, actIcon, actLabel: activity, who: bubbleWho });
    if (activity) { clearTimeout(bubbleTimer); patch("bubble", { show: true }); }
  }

  function hideBubbleSoon(ms = 7000) {
    clearTimeout(bubbleTimer);
    if (S.toolsPending.size || els.floatDock?.matches(":hover")) return; // new speech cancels this anyway
    bubbleTimer = later(() => patch("bubble", { show: false }), ms);
  }

  function dockHover(on: boolean) {
    if (on) { clearTimeout(bubbleTimer); if (get().bubble.text) patch("bubble", { show: true }); }
    else hideBubbleSoon(2500);
  }

  function handleLive(msg: LiveMessage) {
    switch (msg.type) {
      case "transcript":
        if (msg.role === "user") {
          if (!S.user) { tracker.newTurn(); resumeFollow(false); heardCaption = ""; } // you asked something: follow again
          if (S.model && S.model.text) finalizeModel();
          if (!S.user) S.user = { id: addUser("", true), text: "" };
          S.user.text += msg.text;
          const shown = S.user.text.trimStart();
          patchMsg(S.user.id, (m) => ({ ...m, text: shown }));
          caption(S.user.text, "user");
        } else {
          finalizeUser();
          const m = ensureModel();
          m.text = joinText(m.text, msg.text);
          const shown = m.text.trimStart();
          patchMsg(m.id, (x) => ({ ...x, text: shown }));
          // Captions, the floating bubble and follow-along advance as the words are heard, not when they
          // arrive (they arrive seconds early, and words cut off by an interruption are never heard at all).
          const piece = msg.text;
          atPlayback(() => {
            heardCaption = joinText(heardCaption, piece);
            caption(heardCaption, "model");
            hearSpeech(piece);
          });
        }
        scrollToEnd();
        break;
      case "tool_call": {
        finalizeUser();
        const m = ensureModel();
        const [ic, label] = (TOOL_LABELS[msg.name] || (() => ["sparkles", msg.name] as [string, string]))(msg.args || {});
        const key = `t${msg.id}`;
        patchMsg(m.id, (x) => ({ ...x, acts: [...x.acts, { key, icon: ic, label, running: true, error: null, ui: null }] }));
        m.acts.set(msg.id, key);
        patch("stage", { activity: label });
        S.toolsPending.set(msg.id, msg.name);
        patch("bubble", { show: true });
        paintVoice();
        scrollToEnd();
        break;
      }
      case "tool_result": {
        S.toolsPending.delete(msg.id);
        const key = S.model?.acts.get(msg.id);
        if (S.model && key) {
          patchMsg(S.model.id, (x) => ({
            ...x,
            acts: x.acts.map((a) => (a.key !== key ? a : {
              ...a, running: false, error: msg.error || null, ui: msg.ui || null,
              label: msg.ui?.kind === "progress" ? refLabel(msg.ui)[1] : a.label,
            })),
          }));
        }
        if (msg.ui?.kind === "passage") onPassage(msg.ui);
        if (msg.ui?.kind === "progress") refreshProgress();
        if (msg.ui?.kind === "search" || msg.ui?.kind === "passage") followMaterial(msg.ui);
        if (!S.toolsPending.size) patch("stage", { activity: msg.ui?.kind === "passage" ? `${msg.ui.section} · p. ${msg.ui.pages}` : "" });
        paintVoice();
        break;
      }
      case "turn_complete":
        S.turnHasTools = false;
        atPlayback(() => { clearTimeout(retryTimer); tracker.newTurn(); heardCaption = ""; hideBubbleSoon(); });
        finalizeTurn();
        refreshProgressSoon();
        break;
      case "interrupted":
        cancelPlaybackTimers(); // what was queued will never be heard
        S.turnHasTools = false;
        if (S.model?.text) patchMsg(S.model.id, (x) => ({ ...x, interrupted: true }));
        finalizeModel();
        break;
      case "notice":
        toast(msg.text, "info");
        break;
      case "status":
        if (msg.state === "closed") { finalizeTurn(); hideBubbleSoon(2500); }
        break;
    }
  }

  async function sendText(text: string) {
    if (!text.trim() || !live) return;
    tracker.newTurn();
    resumeFollow(false);
    if (!(await ensureReady())) return;
    finalizeTurn();
    addUser(text, false, new Date().toISOString());
    scrollToEnd(true);
    await live.sendText(text);
  }

  async function clearChat() {
    if (!nb.history.length && !get().messages.length) return;
    const ok = await confirmDialog({ title: "Clear the conversation?", body: "This removes the chat history for this notebook. Your sources and reading progress stay.", confirm: "Clear" });
    if (!ok) return;
    try {
      await api.clearHistory(id);
      nb = { ...nb, history: [] };
      S.user = S.model = null;
      store.set({ messages: [] });
      toast("Conversation cleared", "success");
    } catch (e) { toast((e as Error).message, "error"); }
  }

  // ================================================================ sources & outline

  function setNb(next: Notebook) {
    nb = next;
    const { history: _h, jobs: _j, ...rest } = next;
    store.set({ nb: rest });
  }

  async function rename(name: string): Promise<boolean> {
    try {
      const summary = await api.updateNotebook(id, { name });
      setNb({ ...nb, ...summary });
      document.title = `${summary.name} · Voice Tutor`;
      toast("Renamed", "success", 1800);
      return true;
    } catch (e) { toast((e as Error).message, "error"); return false; }
  }

  async function setIcon(icon: string) {
    try {
      await api.updateNotebook(id, { icon });
      setNb({ ...nb, icon });
    } catch (e) { toast((e as Error).message, "error"); }
  }

  async function removeSource(docId: string, title: string) {
    const ok = await confirmDialog({ title: `Remove “${title}”?`, body: "It will be removed from this notebook. If no other notebook uses it, its index and reading progress are deleted too.", confirm: "Remove" });
    if (!ok) return;
    try { await api.removeSource(id, docId); await reload(); toast("Source removed", "success"); } catch (err) { toast((err as Error).message, "error"); }
  }

  async function dismissJob(jobId: string) {
    await api.dismissJob(jobId).catch(() => {});
    store.set((s) => ({ jobs: s.jobs.filter((j) => j.id !== jobId) }));
  }

  async function setActive(docId: string) {
    if (docId === nb.active_source) { pauseFollow(); showPage(docId, currentPageFor(docId)); revealViewer(); return; }
    try {
      await api.updateNotebook(id, { active_source: docId });
      setNb({ ...nb, active_source: docId });
      await loadOutline();
      showPage(docId, currentPageFor(docId));
    } catch (e) { toast((e as Error).message, "error"); }
  }

  function currentPageFor(docId: string) {
    const s = nb.sources.find((x) => x.doc_id === docId);
    const pages = s?.position?.started ? s.position.pages : null;
    return pages ? parseInt(String(pages).split("-")[0], 10) || 1 : 1;
  }

  async function loadOutline() {
    const docId = nb.active_source;
    if (!docId) { store.set({ outline: null, outlineError: null }); return; }
    try { store.set({ outline: await api.outline(docId), outlineError: null }); }
    catch (e) { store.set({ outlineError: (e as Error).message }); }
  }

  function outlineMenu(anchor: Element) {
    openMenu(anchor, [
      { label: "Start from the beginning", icon: "play", action: () => sendText("Teach me from the beginning.") },
      { label: "Mark all as completed", icon: "listChecks", action: () => markAll(true) },
      { label: "Reset progress", icon: "reset", action: resetProgress },
    ]);
  }

  async function resetProgress() {
    const o = get().outline;
    const ok = await confirmDialog({ title: "Reset progress?", body: `Start “${o?.title || "this source"}” from scratch: every section goes back to not started and the reading position to the beginning.`, confirm: "Reset", danger: false });
    if (!ok || !nb.active_source) return;
    try { store.set({ outline: await api.resetProgress(nb.active_source) }); await reloadSources(); toast("Progress reset", "success"); } catch (e) { toast((e as Error).message, "error"); }
  }

  function teachSection(sec: OutlineSection) {
    if (!nb.active_source) return;
    resumeFollow(false);
    showPage(nb.active_source, parseInt(sec.pages, 10) || 1);
    const n = nb.sources.findIndex((s) => s.doc_id === nb.active_source) + 1;
    const where = nb.sources.length > 1 ? ` of source ${n}` : "";
    sendText(`Teach me the section “${sec.title}” (section id ${sec.id})${where}.`);
  }

  async function refreshProgress() {
    clearTimeout(progressTimer);
    await reloadSources();
    if (nb.active_source) await loadOutline();
  }

  // Ticks in the outline: click a section's circle to mark it completed / not completed;
  // shift-click marks everything up to and including it.
  async function toggleSection(sec: OutlineSection, upToHere: boolean) {
    const docId = nb.active_source;
    if (!docId) return;
    const completed = upToHere || sec.status !== "completed";
    try {
      store.set({ outline: await api.markProgress(docId, sec.id, completed, upToHere) });
      await reloadSources();
    } catch (e) { toast((e as Error).message, "error"); }
  }

  async function markAll(completed: boolean) {
    const docId = nb.active_source;
    if (!docId) return;
    try {
      store.set({ outline: await api.markProgress(docId, "all", completed) });
      await reloadSources();
      toast(completed ? "Marked everything as completed" : "Cleared all progress", "success");
    } catch (e) { toast((e as Error).message, "error"); }
  }

  let progressTimer: ReturnType<typeof setTimeout> | undefined;
  function refreshProgressSoon() {
    clearTimeout(progressTimer);
    progressTimer = later(async () => { await reloadSources(); if (nb.active_source) await loadOutline(); }, 600);
  }

  async function reloadSources() {
    try {
      const fresh = await api.notebook(id);
      setNb({ ...nb, sources: fresh.sources, active_source: fresh.active_source });
      mergeJobs(fresh.jobs || []);
    } catch { /* transient */ }
  }

  async function reload() {
    const fresh = await api.notebook(id);
    setNb({ ...fresh, history: nb.history });
    await loadOutline();
    if (!nb.sources.some((s) => s.doc_id === S.viewerDocId)) {
      S.viewerDocId = null;
      if (nb.active_source) showPage(nb.active_source, currentPageFor(nb.active_source)); else renderViewerEmpty();
    }
  }

  // ================================================================ uploads

  function pickFiles() { (els.fileInput as HTMLInputElement | undefined)?.click(); }

  async function upload(files: File[]) {
    const pdfs = files.filter((f) => f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf"));
    if (!pdfs.length) { toast("Only PDF files can be added.", "error"); return; }
    if (pdfs.length < files.length) toast("Skipped files that aren't PDFs.", "info");
    try {
      const { jobs } = await api.upload(id, pdfs);
      store.set((s) => ({ jobs: [...s.jobs.filter((j) => !jobs.some((n) => n.id === j.id)), ...jobs] }));
      pollJobs();
    } catch (e) { toast((e as Error).message, "error", 6000); }
  }

  // Jobs the server started on its own (re-indexing after an embedding-model switch) show up here too.
  function mergeJobs(jobs: Job[]) {
    const known = new Set(get().jobs.map((j) => j.id));
    const fresh = jobs.filter((j) => !known.has(j.id) && (j.status === "queued" || j.status === "running"));
    if (!fresh.length) return;
    store.set((s) => ({ jobs: [...s.jobs, ...fresh] }));
    pollJobs();
  }

  let polling = false;
  async function pollJobs() {
    if (polling || S.disposed) return;
    polling = true;
    try {
      while (!S.disposed && get().jobs.some((j) => j.status === "queued" || j.status === "running")) {
        await new Promise<void>((r) => later(r, 700));
        for (const j of get().jobs) {
          if (j.status !== "queued" && j.status !== "running") continue;
          try {
            const fresh = await api.job(j.id);
            if (fresh.status === "done") {
              store.set((s) => ({ jobs: s.jobs.filter((x) => x.id !== j.id) }));
              api.dismissJob(j.id).catch(() => {});
              if (fresh.kind === "reindex") {
                toast(`Search is fully ready for “${fresh.title || fresh.file_name}”`, "success");
                await reloadSources();
              } else {
                toast(`Added “${fresh.title || fresh.file_name}”`, "success");
                await reload();
              }
            } else {
              store.set((s) => ({ jobs: s.jobs.map((x) => (x.id === j.id ? fresh : x)) }));
            }
          } catch { store.set((s) => ({ jobs: s.jobs.filter((x) => x.id !== j.id) })); }
        }
      }
    } finally { polling = false; }
  }

  // ================================================================ viewer

  function renderViewerEmpty() {
    S.viewerDocId = null;
    patch("viewer", { docId: null, title: null, page: null, total: null });
    pdf?.empty(h("div", { class: "viewer-empty" }, h("span", { class: "empty-ic" }, iconEl("file")),
      h("b", {}, "No page yet"), h("span", {}, "Pages appear here as we go through your sources.")));
  }

  function openDoc(docId: string, title: string) {
    S.viewerDocId = docId;
    patch("viewer", { docId, title });
  }

  // Scroll the viewer to a page of a source (opening that source first if needed) and highlight the
  // passage being discussed.
  let showToken = 0;
  async function showPage(docId: string | null, page: number, snippet: string | null = null) {
    if (!docId || !pdf) return;
    const src = nb.sources.find((s) => s.doc_id === docId);
    if (!src || src.missing) return;
    const token = ++showToken;
    if (docId !== S.viewerDocId || !pdf.total) {
      openDoc(docId, src.title);
      if (!(await pdf.open(docId, src.pages || 1)) || token !== showToken) return;
    }
    page = Math.min(Math.max(1, page || 1), pdf.total);
    pdf.clearHighlight();
    pdf.goTo(page);
    if (!snippet) return;
    try {
      const sentence = snippet.split(/(?<=[.!?])\s+/).find((x) => x.split(" ").length >= 5) || snippet;
      const loc = await api.locate(docId, sentence, page, page + 1);
      if (token !== showToken || S.viewerDocId !== docId || !loc.page) return;
      pdf.highlight(loc.page, loc.rects);
    } catch { /* highlight is best-effort */ }
  }

  function onPassage(ui: Extract<ToolUI, { kind: "passage" }>) {
    if (!ui.text && S.follow && !S.followPaused) showPage(ui.doc_id, ui.page, ui.snippet); // older server: no text to follow
    if (ui.doc_id !== nb.active_source) setNb({ ...nb, active_source: ui.doc_id });
    refreshProgressSoon();
  }

  function openCitation(docId: string, page: number, snippet: string) {
    pauseFollow();
    showPage(docId, page, snippet);
    revealViewer();
  }

  function goToPage(n: number) { pauseFollow(); pdf?.goTo(n); }
  function prevPage() { pauseFollow(); pdf?.prev(); }
  function nextPage() { pauseFollow(); pdf?.next(); }
  function zoomIn() { pdf?.zoomIn(); }
  function zoomOut() { pdf?.zoomOut(); }
  function fitWidth() { pdf?.setZoom(1); }

  // ================================================================ follow along
  // The page follows what the tutor is *saying*: tool results bring in the passages it's working from, the
  // tracker (follow.ts) matches its speech to their sentences, and each step is timed to when that audio is
  // actually heard. Scrolling or jumping yourself pauses following (a pill brings you back); asking
  // something new resumes it.

  const playbackTimers = new Set<ReturnType<typeof setTimeout>>();

  function atPlayback(fn: () => void) {
    const lag = (live?.playbackLag || 0) * 1000;
    if (lag < 40) { fn(); return; }
    const t = setTimeout(() => { playbackTimers.delete(t); if (!S.disposed) fn(); }, lag);
    playbackTimers.add(t);
  }

  function cancelPlaybackTimers() {
    playbackTimers.forEach(clearTimeout);
    playbackTimers.clear();
  }

  const lastPage = (pages: string | undefined, first: number) => parseInt(String(pages || "").split("-").pop() || "", 10) || first;

  function followMaterial(ui: Extract<ToolUI, { kind: "search" | "passage" }>) {
    const cands: Candidate[] = ui.kind === "passage"
      ? [{ doc_id: ui.doc_id, first: ui.page, last: lastPage(ui.pages, ui.page), text: ui.text, boost: 1.15 }]
      : (ui.refs || []).map((r: SearchRef) => ({ doc_id: r.doc_id, first: r.page, last: lastPage(r.pages, r.page), text: r.text }));
    if (!cands.some((c) => c.text)) return;
    const append = S.turnHasTools; // several lookups in one answer: follow across all of them
    S.turnHasTools = true;
    atPlayback(() => {
      const first = tracker.setCandidates(cands, append);
      // A passage being taught shows its opening right away. Search hits don't move anything yet: the top
      // hit is often not the one the answer uses, so the tutor's words decide.
      if (first >= 0 && ui.kind === "passage") focusSentence(tracker.start(first));
    });
  }

  let retryTimer: ReturnType<typeof setTimeout> | undefined;
  function hearSpeech(piece: string) {
    focusSentence(tracker.hear(piece));
    clearTimeout(retryTimer);
    if (tracker.retryAt) retryTimer = later(() => hearSpeech(""), Math.max(30, tracker.retryAt - performance.now() + 20));
  }

  let focusToken = 0;
  async function focusSentence(seg: Segment | null) {
    if (!seg || !S.follow) return;
    const token = ++focusToken;
    let loc;
    try { loc = await api.locate(seg.doc_id, seg.text, seg.first, seg.last); } catch { return; }
    if (token !== focusToken || S.disposed || !loc.page) return; // not found on the page: keep the current spot
    const same = S.focus && S.focus.doc_id === seg.doc_id && S.focus.page === loc.page
      && JSON.stringify(S.focus.rects) === JSON.stringify(loc.rects);
    if (same && S.viewerDocId === seg.doc_id) return; // same line (overlapping hits): no flicker
    S.focus = { doc_id: seg.doc_id, page: loc.page, rects: loc.rects };
    await showFocus(!S.followPaused);
    paintFollowPill();
  }

  // move=false: repaint the highlight where it is without scrolling (following is paused).
  async function showFocus(move: boolean) {
    const f = S.focus;
    if (!f || !pdf) return;
    const src = nb.sources.find((x) => x.doc_id === f.doc_id);
    if (!src || src.missing) return;
    if (f.doc_id !== S.viewerDocId) {
      if (!move) return;
      ++showToken;
      openDoc(f.doc_id, src.title);
      if (!(await pdf.open(f.doc_id, src.pages || 1)) || S.focus !== f) return;
    }
    pdf.highlight(f.page, f.rects, move ? "reveal" : false);
  }

  function pauseFollow() {
    if (!S.follow || S.followPaused) return;
    S.followPaused = true;
    paintFollowPill();
  }

  function resumeFollow(jump: boolean) {
    const was = S.followPaused;
    S.followPaused = false;
    paintFollowPill();
    if (jump || (was && live?.speaking)) showFocus(true);
  }

  function setFollow(on: boolean) {
    S.follow = on;
    S.followPaused = false;
    store.set({ follow: on });
    paintFollowPill();
    if (on) showFocus(true);
  }

  function paintFollowPill() {
    const f = S.focus;
    if (!(S.follow && S.followPaused && f && live?.active)) { store.set({ pill: null }); return; } // only matters mid-conversation
    const multi = nb.sources.length > 1;
    const n = nb.sources.findIndex((x) => x.doc_id === f.doc_id) + 1;
    store.set({ pill: `Back to the tutor · ${multi && n ? `source ${n}, ` : ""}p. ${f.page}` });
  }

  // ================================================================ layout: viewer, tabs, splits

  function toggleViewer() {
    if (S.reading) { setReading(false); return; }
    const l = get().layout;
    if (matches(VIEWPORT.phone)) { setTab(l.tab === "page" ? "chat" : "page"); return; }
    if (matches(VIEWPORT.narrow)) patch("layout", { viewerOpen: !l.viewerOpen });
    else patch("layout", { viewerHidden: !l.viewerHidden });
    paintViewerToggle();
    requestAnimationFrame(applySplit);
  }

  function revealViewer() {
    if (S.reading) return;
    if (matches(VIEWPORT.phone)) setTab("page");
    else if (matches(VIEWPORT.narrow)) patch("layout", { viewerOpen: true });
    else patch("layout", { viewerHidden: false });
    paintViewerToggle();
    requestAnimationFrame(applySplit);
  }

  function paintViewerToggle() {
    const l = get().layout;
    patch("layout", { toggleActive: matches(VIEWPORT.narrow) ? l.viewerOpen : !l.viewerHidden });
  }

  function setTab(tab: Tab) { patch("layout", { tab }); }

  const split = { sources: SOURCES.def, viewer: VIEWER.def, collapsed: false };
  const sized = { sources: split.sources, viewer: 0, room: 0 };

  function loadSplit() {
    try {
      const saved = JSON.parse(localStorage.getItem(SPLIT_KEY) || "{}");
      if (Number.isFinite(saved.sources)) split.sources = saved.sources;
      if (Number.isFinite(saved.viewer) && saved.viewer > 0 && saved.viewer < 1) split.viewer = saved.viewer;
      split.collapsed = saved.collapsed === true;
    } catch { /* storage blocked */ }
  }

  function saveSplit() {
    try { localStorage.setItem(SPLIT_KEY, JSON.stringify(split)); } catch { /* storage blocked */ }
  }

  function applySplit() {
    const body = els.body;
    if (!body || matches(VIEWPORT.phone)) return;
    const viewerInGrid = !get().layout.viewerHidden && matches(VIEWPORT.wide);
    const room = body.clientWidth - 24 - GUTTER * (viewerInGrid ? 2 : 1); // minus padding and gutters
    const sourcesRoom = Math.max(SOURCES.min, room - CHAT_MIN - (viewerInGrid ? VIEWER.min : 0));
    const sources = split.collapsed ? 0 : Math.round(Math.min(Math.max(split.sources, SOURCES.min), SOURCES.max, sourcesRoom));
    let viewer = 0;
    if (viewerInGrid) viewer = Math.round(Math.max(VIEWER.min, Math.min(split.viewer * room, room - sources - CHAT_MIN)));
    Object.assign(sized, { sources, viewer, room });
    if (get().layout.collapsed !== split.collapsed) patch("layout", { collapsed: split.collapsed });
    body.style.setProperty("--w-sources", `${sources}px`);
    body.style.setProperty("--w-viewer", `${viewer}px`);
    els.gutterSources?.setAttribute("aria-valuenow", String(sources));
    els.gutterViewer?.setAttribute("aria-valuenow", String(viewer));
  }

  function setSources(px: number) {
    if (px < SOURCES.collapseBelow) split.collapsed = true;
    else { split.collapsed = false; split.sources = Math.min(Math.max(px, SOURCES.min), SOURCES.max); }
    applySplit();
  }

  function setViewerWidth(px: number) {
    if (!sized.room) return;
    split.viewer = Math.min(0.9, Math.max(0.1, px / sized.room));
    applySplit();
    split.viewer = sized.viewer / sized.room; // remember what actually fits, not how far the mouse went
  }

  function resetSplit(which: "sources" | "viewer") {
    if (which === "sources") { split.sources = SOURCES.def; split.collapsed = false; } else split.viewer = VIEWER.def;
    applySplit();
    saveSplit();
  }

  function gutterPointerDown(which: "sources" | "viewer", e: ReactPointerEvent<HTMLElement>) {
    if (e.button !== 0) return;
    e.preventDefault();
    const handle = e.currentTarget;
    try { handle.setPointerCapture(e.pointerId); } catch { /* pointer already gone */ }
    const startX = e.clientX;
    const start = which === "sources" ? sized.sources : sized.viewer;
    let moved = false;
    patch("layout", { resizing: true, dragging: which });
    const move = (ev: PointerEvent) => {
      const dx = ev.clientX - startX;
      if (!moved && Math.abs(dx) < 3) return;
      moved = true;
      if (which === "sources") setSources(start + dx); else setViewerWidth(start - dx);
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
      patch("layout", { resizing: false, dragging: null });
      if (!moved && which === "sources" && split.collapsed) { split.collapsed = false; applySplit(); } // click to reopen
      saveSplit();
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  }

  function gutterKeyDown(which: "sources" | "viewer", e: ReactKeyboardEvent<HTMLElement>) {
    const step = e.shiftKey ? 64 : 24;
    const dir = e.key === "ArrowLeft" ? -1 : e.key === "ArrowRight" ? 1 : 0;
    if (dir) {
      e.preventDefault();
      e.stopPropagation();
      if (which === "sources") setSources((split.collapsed ? SOURCES.min - step : sized.sources) + dir * step);
      else setViewerWidth(sized.viewer - dir * step);
      saveSplit();
    } else if (e.key === "Enter" && which === "sources") {
      e.preventDefault();
      split.collapsed = !split.collapsed;
      applySplit();
      saveSplit();
    }
  }

  // ================================================================ full-screen reading mode
  // The PDF fills the screen (real browser full screen when allowed) and scrolls freely; the tutor
  // floats at the side as the voice orb, with a bubble showing what it says.

  function setReading(on: boolean) {
    if (on === S.reading) return;
    S.reading = on;
    store.set({ reading: on });
    stageOrb?.setPaused(on);
    paintStage();
    if (on) {
      // requestFullscreen needs the click/keypress that got us here, so it comes before anything async.
      if (!document.fullscreenElement && document.documentElement.requestFullscreen) {
        document.documentElement.requestFullscreen({ navigationUI: "hide" }).catch(() => { /* not allowed: fill the window instead */ });
      }
      floatOrb = new FluidOrb({ size: 60, state: S.voiceKey });
      els.floatOrbBtn?.prepend(floatOrb.el);
      paintVoice();
      if (!live?.active && !nb.history.length) {
        bubbleSay("hint", "Tap me to talk about what you're reading. I'll follow along on the page.");
        hideBubbleSoon(6000);
      }
      if (!S.viewerDocId && nb.active_source) showPage(nb.active_source, currentPageFor(nb.active_source));
      later(() => pdf?.focus(), 60);
      wakeChrome();
    } else {
      wakeChrome();
      floatOrb?.destroy();
      floatOrb = null;
      els.floatOrbBtn?.querySelector(".fluid-orb")?.remove();
      toggleComposer(false);
      patch("bubble", { show: false });
      if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
      requestAnimationFrame(applySplit);
    }
  }

  // The floating toolbars fade out while you read and come back when the mouse moves.
  let idleTimer: ReturnType<typeof setTimeout> | undefined;
  function wakeChrome() {
    if (get().idle) store.set({ idle: false });
    clearTimeout(idleTimer);
    if (!S.reading) return;
    idleTimer = later(() => {
      const busy = els.viewerPanel?.querySelector(".viewer-head:hover, .viewer-foot:hover, .viewer-head:focus-within, .viewer-foot:focus-within");
      if (S.reading && !busy) store.set({ idle: true }); else wakeChrome();
    }, 2600);
  }

  function viewerPointer() { if (S.reading) wakeChrome(); }

  function toggleComposer(open = !get().composerOpen) {
    store.set({ composerOpen: open });
    if (open) requestAnimationFrame(() => els.floatInput?.focus());
    else if (S.reading) pdf?.focus();
  }

  function sendFloat(text: string) {
    if (!text.trim()) return;
    bubbleSay("user", text.trim());
    sendText(text.trim());
  }

  function focusComposer() { els.textarea?.focus(); }

  // ================================================================ mount / dispose

  function mount() {
    S.disposed = false;
    loadSplit();
    pollJobs(); // jobs already running when the page opened
    try { S.view = localStorage.getItem("vt-voice-view") === "orb" ? "orb" : "transcript"; } catch { /* storage blocked */ }
    store.set({ view: S.view });

    live = new LiveClient(id, {
      onMessage: handleLive,
      onState: paintVoice,
      onLevel: setLevel,
      onError: (message, fatal) => {
        toast(message, "error", 6500);
        if (fatal) finalizeTurn();
      },
    });

    dockOrb = new FluidOrb({ size: 42 });
    els.orbBtn?.prepend(dockOrb.el);

    if (els.stage) {
      pdf = new PdfView({
        el: els.stage,
        onPage: (n, total) => patch("viewer", { page: n, total }),
        onZoom: (zoom) => patch("viewer", { zoom }),
        onUserScroll: () => pauseFollow(),
      });
    }

    // Window-level listeners: drag & drop, keyboard, full screen, settings changes.
    let dragDepth = 0;
    const hasFiles = (e: DragEvent) => [...(e.dataTransfer?.types || [])].includes("Files");
    const onDragEnter = (e: DragEvent) => { if (!hasFiles(e)) return; e.preventDefault(); dragDepth++; store.set({ drop: true }); };
    const onDragOver = (e: DragEvent) => { if (hasFiles(e)) e.preventDefault(); };
    const onDragLeave = () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) store.set({ drop: false }); };
    const onDrop = (e: DragEvent) => {
      if (!e.dataTransfer?.files?.length) return;
      e.preventDefault();
      dragDepth = 0;
      store.set({ drop: false });
      upload([...e.dataTransfer.files]);
    };
    const typing = (el: Element | null) => !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || (el as HTMLElement).isContentEditable);
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey || document.querySelector(".overlay")) return;
      if (typing(document.activeElement)) return;
      if (e.key === "/") { e.preventDefault(); if (S.reading) toggleComposer(true); else focusComposer(); }
      else if ((e.key === "m" || e.key === "M") && live?.active) { e.preventDefault(); live.toggleMic(); }
      else if ((e.key === "f" || e.key === "F") && (S.viewerDocId || S.reading)) { e.preventDefault(); setReading(!S.reading); }
      else if (e.key === "Escape" && S.reading) { e.preventDefault(); setReading(false); }
      else if (e.key === "Escape" && live?.active) { e.preventDefault(); live.end(); }
      else if (e.key === "ArrowLeft" && pdf?.total) { e.preventDefault(); prevPage(); }
      else if (e.key === "ArrowRight" && pdf?.total) { e.preventDefault(); nextPage(); }
    };
    // Leaving full screen with the browser's own Esc / F11 also leaves reading mode.
    const onFullscreen = () => { if (!document.fullscreenElement && S.reading) setReading(false); };
    const onSettings = (e: Event) => {
      if (live?.active) toast(`End and restart the conversation to hear ${(e as CustomEvent).detail.voice}.`, "info", 5000);
    };
    window.addEventListener("dragenter", onDragEnter);
    window.addEventListener("dragover", onDragOver);
    window.addEventListener("dragleave", onDragLeave);
    window.addEventListener("drop", onDrop);
    document.addEventListener("keydown", onKey);
    document.addEventListener("fullscreenchange", onFullscreen);
    window.addEventListener("vt-settings", onSettings);
    cleanups.push(() => {
      window.removeEventListener("dragenter", onDragEnter);
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("dragleave", onDragLeave);
      window.removeEventListener("drop", onDrop);
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("fullscreenchange", onFullscreen);
      window.removeEventListener("vt-settings", onSettings);
    });

    const stageResize = new ResizeObserver(() => { if (get().stage.show) fitStage(); });
    if (els.voiceStage) stageResize.observe(els.voiceStage);
    const bodyResize = new ResizeObserver(() => { applySplit(); paintViewerToggle(); });
    if (els.body) bodyResize.observe(els.body);
    cleanups.push(() => { stageResize.disconnect(); bodyResize.disconnect(); });

    // Boot: the conversation opens at its latest message, the viewer on the page being taught.
    S.wantScroll = true;
    flushScroll();
    applySplit();
    paintViewerToggle();
    paintVoice();
    loadOutline().then(() => {
      if (S.disposed) return;
      if (nb.active_source) showPage(nb.active_source, currentPageFor(nb.active_source)); else renderViewerEmpty();
    });
    if (get().jobs.length) pollJobs();
    refreshOnOpen();
  }

  // The page may come from the browser's back/forward cache: make sure what's shown is current.
  async function refreshOnOpen() {
    let fresh: Notebook;
    try { fresh = await api.notebook(id); } catch { return; }
    if (S.disposed) return;
    const last = (h: HistoryEntry[]) => (h.length ? `${h.length}:${h[h.length - 1].ts}` : "0");
    const historyChanged = last(fresh.history) !== last(nb.history);
    setNb(fresh);
    store.set((s) => ({ jobs: [...s.jobs.filter((j) => !fresh.jobs.some((f) => f.id === j.id)), ...fresh.jobs] }));
    if (historyChanged && !S.user && !S.model && !live?.active) {
      S.wantScroll = true;
      store.set({ messages: historyMessages(fresh.history) });
    }
    if (get().jobs.some((j) => j.status === "queued" || j.status === "running")) pollJobs();
  }

  function dispose() {
    S.disposed = true;
    if (S.reading && document.fullscreenElement) document.exitFullscreen().catch(() => {});
    S.reading = false;
    cancelPlaybackTimers();
    live?.end();
    live = null;
    dockOrb?.destroy();
    dockOrb?.el.remove();
    dockOrb = null;
    stageOrb?.destroy();
    stageOrb = null;
    els.stageOrb?.replaceChildren();
    liveOrb?.destroy();
    liveOrb?.el.remove();
    liveOrb = null;
    floatOrb?.destroy();
    floatOrb?.el.remove();
    floatOrb = null;
    pdf?.destroy();
    pdf = null;
    S.viewerDocId = null;
    timers.forEach(clearTimeout);
    timers.clear();
    clearTimeout(progressTimer);
    clearTimeout(bubbleTimer);
    clearTimeout(idleTimer);
    clearTimeout(retryTimer);
    cleanups.splice(0).forEach((fn) => fn());
    polling = false;
    S.user = S.model = null;
    S.toolsPending.clear();
  }

  return {
    store, ref, mount, dispose, flushScroll,
    // conversation
    sendText, clearChat, onOrb, setView,
    toggleMic: () => live?.toggleMic(),
    endLive: () => live?.end(),
    // sources & outline
    pickFiles, upload, removeSource, dismissJob, setActive, teachSection, toggleSection, outlineMenu, rename, setIcon,
    // viewer
    toggleViewer, setTab, openCitation, goToPage, prevPage, nextPage, zoomIn, zoomOut, fitWidth, setFollow,
    resumeFollow: () => resumeFollow(true), viewerPointer,
    currentPage: () => pdf?.page ?? null,
    // splits
    gutterPointerDown, gutterKeyDown, resetSplit,
    // reading mode
    toggleReading: () => setReading(!S.reading), toggleComposer, sendFloat, dockHover,
  };
}
