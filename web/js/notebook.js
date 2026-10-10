// Notebook view: sources + outline | conversation + voice | PDF viewer, with draggable splits between them,
// and a full-screen reading mode where the PDF fills the screen and the tutor becomes a floating orb.
import { api } from "./api.js";
import { FluidOrb } from "./fluid-orb.js";
import { FollowTracker } from "./follow.js";
import { markIcon } from "./mark.js";
import { LiveClient } from "./live.js";
import { PdfView } from "./pdf-view.js";
import { openSettings, refreshStatus, shared, statusPill } from "./main.js";
import { clockTime, confirmDialog, copyText, h, icon, modal, NOTEBOOK_ICONS, notebookIcon, openMenu, ring, toast } from "./ui.js";

const TOOL_LABELS = {
  get_outline: () => ["list", "Checking the outline"],
  read_passage: (a) => ["book", passageLabel(String(a.target || "next").trim())],
  search_sources: (a) => ["search", `Searching your sources for “${a.query || ""}”`],
  web_search: (a) => ["globe", `Searching the web for “${a.query || ""}”`],
};

// Append a transcript piece, adding the space that goes missing when speech resumes after a tool call.
function joinText(previous, piece) {
  if (previous && piece && !/\s$/.test(previous) && !/^\s/.test(piece) && /[.!?,;:]$/.test(previous)) return `${previous} ${piece}`;
  return previous + piece;
}

function passageLabel(target) {
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

const QUICK = [
  ["start", "play", "Start lesson", "Teach me from the beginning."],
  ["continue", "play", "Continue", "Continue where we left off."],
  ["quiz", "messageQuestion", "Quiz me", "Quiz me on what we just covered, one question at a time."],
  ["summary", "listChecks", "Summarize", "Summarize the current section, covering every key point."],
  ["web", "globe", "What's new?", "Search the web for anything newer or different from what my sources say."],
];

export async function renderNotebook(root, id) {
  let nb = await api.notebook(id);
  document.title = `${nb.name} · Voice Tutor`;

  const S = {
    outline: null,
    viewer: { docId: null, highlight: null },
    follow: true,
    followPaused: false, // you scrolled or jumped away while following; resumes on your next question
    focus: null, // { doc_id, page, rects }: the spot the tutor is talking about
    turnHasTools: false,
    reading: false,
    toolsPending: new Map(),
    user: null, // live user bubble
    model: null, // live model message
    tab: "chat",
    jobs: new Map(),
    disposed: false,
    voiceKey: "idle",
  };
  const timers = new Set();
  const later = (fn, ms) => { const t = setTimeout(() => { timers.delete(t); fn(); }, ms); timers.add(t); return t; };

  // ================================================================ layout

  const fileInput = h("input", { type: "file", accept: "application/pdf,.pdf", multiple: true, class: "sr-only",
    onchange: () => { if (fileInput.files.length) upload([...fileInput.files]); fileInput.value = ""; } });
  const pickFiles = () => fileInput.click();

  // ---- top bar
  const nameInput = h("input", { class: "nb-name", value: nb.name, "aria-label": "Notebook name", maxlength: 120, spellcheck: "false" });
  nameInput.addEventListener("keydown", (e) => { if (e.key === "Enter") nameInput.blur(); if (e.key === "Escape") { nameInput.value = nb.name; nameInput.blur(); } });
  nameInput.addEventListener("blur", async () => {
    const name = nameInput.value.trim();
    if (!name || name === nb.name) { nameInput.value = nb.name; return; }
    try { nb = { ...nb, ...(await api.updateNotebook(id, { name })) }; document.title = `${nb.name} · Voice Tutor`; toast("Renamed", "success", 1800); }
    catch (e) { toast(e.message, "error"); nameInput.value = nb.name; }
  });
  const nbSub = h("div", { class: "nb-sub" });
  const iconBtn = h("button", { class: "icon-tile btn-tile", "aria-label": "Change icon", "data-tip": "Change icon", onclick: pickIcon }, notebookIcon(nb.icon));
  const livePill = h("span", { class: "live-pill", hidden: true }, h("span", { class: "dot" }), "Live");
  const viewerToggle = h("button", { class: "icon-btn active", "aria-label": "Show or hide the page viewer", "data-tip": "Page viewer", onclick: toggleViewer }, icon("panel"));

  // ---- sources panel
  const sourceCount = h("span", { class: "count-badge" });
  const sourcesList = h("div", { class: "sources-list" });
  const outlineBox = h("div", { class: "outline-wrap" });
  const sourcesPanel = h("section", { class: "panel sources", "aria-label": "Sources" },
    h("div", { class: "panel-head" }, h("h2", {}, "Sources", sourceCount),
      h("button", { class: "btn sm", onclick: pickFiles }, icon("plus", "sm"), "Add")),
    h("div", { class: "panel-body scroll sources-body" }, sourcesList, outlineBox));

  // ---- conversation panel
  const banner = h("div", { class: "banner-slot" });
  const messagesInner = h("div", { class: "messages-inner" });
  const messages = h("div", { class: "messages scroll" }, messagesInner);

  const dockOrb = new FluidOrb({ size: 50 });
  const orbIcon = h("span", { class: "orb-icon" }, icon("mic", "lg"));
  const orb = h("button", { class: "orb-btn", "aria-label": "Start talking", onclick: onOrb }, dockOrb.el, orbIcon);
  const voiceLabel = h("b", {}, "Tap to talk");
  const voiceSub = h("span", {}, "or type a message");
  const endBtn = h("button", { class: "icon-btn danger-soft", "aria-label": "End conversation", "data-tip": "End (Esc)", onclick: () => live.end(), hidden: true }, icon("hangup"));
  const focusBtn = h("button", { class: "icon-btn", "aria-label": "Open voice mode", "data-tip": "Voice mode", hidden: true, onclick: () => setFocus(true) }, icon("sparkles"));
  const textarea = h("textarea", { rows: 1, placeholder: "Ask anything…", "aria-label": "Message" });
  const sendBtn = h("button", { class: "send-btn", disabled: true, "aria-label": "Send message", onclick: sendTyped }, icon("send"));
  textarea.addEventListener("input", () => { autosize(); sendBtn.disabled = !textarea.value.trim(); });
  textarea.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendTyped(); } });
  const quick = h("div", { class: "quick" });

  // ---- voice mode stage
  let stageOrb = null;
  let focusPref = true;
  try { focusPref = localStorage.getItem("vt-voice-focus") !== "off"; } catch { /* storage blocked */ }
  const stageOrbSlot = h("div", { class: "stage-orb" });
  const stageLabel = h("div", { class: "stage-label" }, "");
  const stageActivity = h("div", { class: "stage-activity" }, "");
  const stageCaption = h("div", { class: "stage-caption" }, "");
  const stageMic = h("button", { class: "stage-btn", "aria-label": "Mute microphone", onclick: () => live.toggleMic() }, icon("mic"));
  const stageEnd = h("button", { class: "stage-btn end", "aria-label": "End conversation", onclick: () => live.end() }, icon("hangup"));
  const voiceStage = h("div", { class: "voice-stage", "aria-live": "polite" },
    h("div", { class: "stage-aura", "aria-hidden": "true" }),
    h("div", { class: "stage-top" }, h("button", { class: "btn sm ghost-glass", onclick: () => setFocus(false) }, icon("chat", "sm"), "Show transcript")),
    h("div", { class: "stage-center" }, stageOrbSlot, stageLabel, stageActivity, stageCaption),
    h("div", { class: "stage-controls" },
      h("div", { class: "stage-ctl" }, stageMic, h("span", {}, "Mic")),
      h("div", { class: "stage-ctl" }, stageEnd, h("span", {}, "End"))));

  const chatPanel = h("section", { class: "panel chat", "aria-label": "Conversation" },
    h("div", { class: "panel-head" }, h("h2", {}, "Conversation"),
      h("button", { class: "icon-btn sm", "aria-label": "Clear conversation", "data-tip": "Clear conversation", onclick: clearChat }, icon("trash", "sm"))),
    banner,
    h("div", { class: "panel-body chat-body" },
      h("div", { class: "chat-main" }, messages, voiceStage),
      h("div", { class: "dock" }, h("div", { class: "dock-inner" }, quick,
        h("div", { class: "dock-card" },
          h("div", { class: "voice" }, orb, h("div", { class: "voice-state" }, voiceLabel, voiceSub)),
          h("div", { class: "composer" }, textarea, sendBtn),
          h("div", { class: "dock-actions" }, focusBtn, endBtn))))));

  // ---- viewer panel: continuous vertical scroll through the whole PDF
  const docName = h("div", { class: "doc-name" }, "Page viewer");
  const zoomLabel = h("button", { class: "zoom-label", "aria-label": "Fit to width", "data-tip": "Fit width", onclick: () => pdf.setZoom(1) }, "100%");
  const openPdfBtn = h("a", { class: "icon-btn sm", "aria-label": "Open the PDF in a new tab", "data-tip": "Open PDF", target: "_blank", rel: "noopener" }, icon("external", "sm"));
  const fullBtn = h("button", { class: "icon-btn sm", "aria-label": "Full-screen reading (F)", "data-tip": "Full screen (F)", onclick: () => setReading(!S.reading) }, icon("maximize", "sm"));
  const closeViewerBtn = h("button", { class: "icon-btn sm viewer-close", "aria-label": "Close viewer", onclick: toggleViewer }, icon("x", "sm"));
  const pageInput = h("input", { class: "page-input", type: "text", inputmode: "numeric", "aria-label": "Page number", value: "" });
  const pageTotal = h("span", { class: "page-total" }, "");
  const pdf = new PdfView({
    onPage: (n, total) => {
      if (document.activeElement !== pageInput) pageInput.value = String(n);
      pageTotal.textContent = `/ ${total}`;
      if (S.viewer.docId) openPdfBtn.href = `${api.fileUrl(S.viewer.docId)}#page=${n}`;
    },
    onZoom: (z) => { zoomLabel.textContent = `${Math.round(z * 100)}%`; },
    onUserScroll: () => pauseFollow(),
  });
  const stage = pdf.el;
  pageInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { const n = parseInt(pageInput.value, 10); if (n) { pauseFollow(); pdf.goTo(n); } pageInput.blur(); }
    if (e.key === "Escape") { pageInput.value = String(pdf.page); pageInput.blur(); }
  });
  pageInput.addEventListener("focus", () => pageInput.select());
  pageInput.addEventListener("blur", () => { pageInput.value = S.viewer.docId ? String(pdf.page) : ""; });
  const followInput = h("input", { type: "checkbox", checked: true, onchange: () => setFollow(followInput.checked) });
  const followPillText = h("span", {}, "Back to the tutor");
  const followPill = h("button", { class: "follow-pill", hidden: true, onclick: () => resumeFollow(true) },
    icon("crosshair", "sm"), followPillText);
  const viewerPanel = h("section", { class: "panel viewer", "aria-label": "Page viewer" },
    h("div", { class: "panel-head viewer-head" }, h("span", { class: "doc-ic" }, icon("file", "sm")), docName,
      h("div", { class: "zoom" },
        h("button", { class: "icon-btn sm", "aria-label": "Zoom out", onclick: () => pdf.zoomOut() }, icon("zoomOut", "sm")),
        zoomLabel,
        h("button", { class: "icon-btn sm", "aria-label": "Zoom in", onclick: () => pdf.zoomIn() }, icon("zoomIn", "sm"))),
      openPdfBtn, fullBtn, closeViewerBtn),
    stage,
    h("div", { class: "viewer-foot" },
      h("button", { class: "icon-btn sm", "aria-label": "Previous page", onclick: () => { pauseFollow(); pdf.prev(); } }, icon("left", "sm")),
      h("span", { class: "page-nav" }, pageInput, pageTotal),
      h("button", { class: "icon-btn sm", "aria-label": "Next page", onclick: () => { pauseFollow(); pdf.next(); } }, icon("right", "sm")),
      h("div", { class: "spacer" }),
      h("label", { class: "follow-toggle", title: "Keep the page on what the tutor is talking about, sentence by sentence" }, followInput, h("span", { class: "switch" }), "Follow along")),
    followPill);

  // ---- draggable splits between the panels
  const gutter = (which, label) => h("div", { class: `gutter g-${which}`, role: "separator", tabindex: "0", "aria-orientation": "vertical",
    "aria-label": label, title: "Drag to resize · double-click to reset" }, h("i"));
  const gutterSources = gutter("sources", "Resize the sources panel");
  const gutterViewer = gutter("viewer", "Resize the page viewer");
  const body = h("div", { class: "nb-body", dataset: { tab: S.tab } }, sourcesPanel, gutterSources, chatPanel, gutterViewer, viewerPanel);

  // ---- reading mode: the tutor as a floating orb with a speech bubble
  let floatOrb = null;
  const floatOrbIcon = h("span", { class: "orb-icon" }, icon("mic", "lg"));
  const floatOrbBtn = h("button", { class: "orb-btn float-orb", "aria-label": "Start talking", onclick: onOrb }, floatOrbIcon);
  const bubbleHead = h("div", { class: "bubble-head" });
  const bubbleText = h("div", { class: "bubble-text" });
  const floatBubble = h("div", { class: "float-bubble", "aria-live": "polite" }, bubbleHead, bubbleText);
  const floatInput = h("textarea", { rows: 1, placeholder: "Ask about this page…", "aria-label": "Message" });
  const floatSend = h("button", { class: "send-btn sm", disabled: true, "aria-label": "Send message", onclick: sendFloat }, icon("send", "sm"));
  const floatComposer = h("div", { class: "float-composer", hidden: true }, floatInput, floatSend);
  const floatType = h("button", { class: "float-btn", "aria-label": "Type a message (/)", "data-tip": "Type (/)", onclick: () => toggleComposer() }, icon("keyboard"));
  const floatEnd = h("button", { class: "float-btn end", "aria-label": "End conversation", "data-tip": "End", hidden: true, onclick: () => live.end() }, icon("hangup"));
  const floatDock = h("div", { class: "float-dock" }, floatBubble,
    h("div", { class: "float-row" }, floatComposer, h("div", { class: "float-actions" }, floatType, floatEnd), floatOrbBtn));
  const fitFloatInput = () => {
    floatInput.style.height = "";
    if (floatInput.value) floatInput.style.height = `${Math.min(floatInput.scrollHeight, 110)}px`;
    floatInput.style.overflowY = floatInput.scrollHeight > 110 ? "auto" : "hidden";
  };
  floatInput.addEventListener("input", () => { floatSend.disabled = !floatInput.value.trim(); fitFloatInput(); });
  floatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendFloat(); }
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); toggleComposer(false); }
  });
  floatDock.addEventListener("mouseenter", () => { clearTimeout(bubbleTimer); if (bubbleText.textContent) floatBubble.classList.add("show"); });
  floatDock.addEventListener("mouseleave", () => hideBubbleSoon(2500));

  const tabs = h("nav", { class: "mobile-tabs" },
    [["sources", "list", "Sources"], ["chat", "chat", "Chat"], ["page", "file", "Page"]].map(([key, ic, label]) =>
      h("button", { class: key === S.tab ? "on" : "", dataset: { tab: key }, onclick: () => setTab(key) }, icon(ic), label)));
  const drop = h("div", { class: "drop-overlay" }, h("div", { class: "drop-card" },
    h("span", { class: "drop-ic" }, icon("upload", "lg")), h("b", {}, "Drop PDFs to add them"), h("span", {}, `to “${nb.name}”`)));

  root.replaceChildren(h("div", { class: "nb" },
    h("header", { class: "topbar" },
      h("button", { class: "icon-btn", "aria-label": "All notebooks", "data-tip": "All notebooks", onclick: () => { location.hash = "#/"; } }, icon("back")),
      h("div", { class: "nb-title" }, iconBtn, h("div", { class: "nb-title-text" }, nameInput, nbSub)),
      h("div", { class: "spacer" }),
      h("div", { class: "topbar-actions" }, livePill, statusPill(), viewerToggle,
        h("button", { class: "icon-btn", "aria-label": "Settings", "data-tip": "Settings", onclick: () => openSettings() }, icon("settings")))),
    body, floatDock, tabs, drop, fileInput));
  const nbEl = root.firstChild;

  // ================================================================ live voice

  const live = new LiveClient(id, {
    onMessage: handleLive,
    onState: paintVoice,
    onLevel: (level) => {
      orb.style.setProperty("--level", level.toFixed(3));
      voiceStage.style.setProperty("--level", level.toFixed(3));
      floatOrbBtn.style.setProperty("--level", level.toFixed(3));
      dockOrb.setLevel(level);
      stageOrb?.setLevel(level);
      floatOrb?.setLevel(level);
    },
    onError: (message, fatal) => {
      toast(message, "error", 6500);
      if (fatal) finalizeTurn();
    },
  });

  async function onOrb() {
    if (!(await ensureReady())) return;
    if (!live.active) await live.start({ mic: true });
    else await live.toggleMic();
  }

  async function ensureReady() {
    const s = shared.status || (await refreshStatus());
    if (!s) { toast("Can't reach the server. Start it with run.bat.", "error"); return false; }
    if (!s.gemini_key) { openSettings(); return false; }
    return true;
  }

  function paintVoice() {
    const st = live.state;
    let label = "Tap to talk", sub = "or type a message", key = "idle", ic = "mic", aria = "Start talking";
    if (st === "connecting") { label = "Connecting…"; sub = "one moment"; key = "connecting"; }
    else if (st === "reconnecting") { label = "Reconnecting…"; sub = "keeping your place"; key = "connecting"; }
    else if (st === "ready") {
      if (S.toolsPending.size) { label = "Looking it up…"; sub = toolHint(); key = "thinking"; }
      else if (live.speaking) { label = "Speaking"; sub = "just talk to interrupt"; key = "speaking"; }
      else if (live.micOn) { label = "Listening"; sub = "go ahead"; key = "listening"; }
      else { label = "Mic off"; sub = "tap to unmute"; key = "muted"; }
      ic = live.micOn ? "mic" : "micOff";
      aria = live.micOn ? "Mute microphone (M)" : "Unmute microphone (M)";
    }
    S.voiceKey = key;
    orb.className = `orb-btn state-${key}`;
    orb.setAttribute("aria-label", aria);
    orbIcon.replaceChildren(icon(ic, "lg"));
    voiceLabel.textContent = label;
    voiceSub.textContent = sub;
    const on = live.active;
    endBtn.hidden = !on;
    livePill.hidden = !on;
    livePill.className = `live-pill state-${key}`;
    if (!on) S.toolsPending.clear();
    stageLabel.textContent = key === "idle" ? "" : label;
    stageMic.replaceChildren(icon(live.micOn ? "mic" : "micOff"));
    stageMic.classList.toggle("off", !live.micOn);
    stageMic.setAttribute("aria-label", live.micOn ? "Mute microphone" : "Unmute microphone");
    voiceStage.dataset.state = key;
    paintStage();
    dockOrb.setState(key);
    stageOrb?.setState(key);
    floatOrb?.setState(key);
    floatOrbBtn.className = `orb-btn float-orb state-${key}`;
    floatOrbBtn.setAttribute("aria-label", aria);
    floatOrbIcon.replaceChildren(icon(ic, "lg"));
    floatEnd.hidden = !on;
    if (S.reading) paintBubbleHead();
    paintFollowPill();
  }

  function toolHint() {
    const names = [...S.toolsPending.values()];
    if (names.includes("web_search")) return "searching the web";
    if (names.includes("search_sources")) return "searching your sources";
    if (names.includes("read_passage")) return "reading";
    return "checking";
  }

  function setFocus(on) {
    focusPref = on;
    try { localStorage.setItem("vt-voice-focus", on ? "on" : "off"); } catch { /* storage blocked */ }
    paintStage();
    if (!on) scrollToEnd(true);
  }

  function paintStage() {
    const show = live.active && focusPref;
    voiceStage.classList.toggle("show", show);
    if (show) fitStage();
    focusBtn.hidden = !(live.active && !focusPref);
    if (!live.active && stageOrb) {
      stageOrb.destroy();
      stageOrb = null;
      stageOrbSlot.replaceChildren();
      stageCaption.textContent = "";
      stageActivity.textContent = "";
    }
  }

  // Size the stage orb to the space actually available (short windows get a small orb, no overlap).
  function fitStage() {
    const height = voiceStage.clientHeight || messages.clientHeight || 400;
    const width = voiceStage.clientWidth || messages.clientWidth || 400;
    voiceStage.classList.toggle("compact", height < 380);
    voiceStage.classList.toggle("tiny", height < 300); // too short for captions: the transcript has them
    const reserved = height < 380 ? 150 : 250; // label, caption, controls
    const size = Math.round(Math.max(56, Math.min(180, height - reserved, width * 0.42)));
    if (stageOrb && Math.abs(stageOrb.size - size) < 8) return;
    stageOrb?.destroy();
    stageOrb = new FluidOrb({ size, className: "stage", state: S.voiceKey });
    stageOrbSlot.replaceChildren(stageOrb.el);
    stageOrb.setPaused(S.reading);
  }
  const stageResize = new ResizeObserver(() => { if (voiceStage.classList.contains("show")) fitStage(); });
  stageResize.observe(voiceStage);

  function caption(text, who) {
    const clean = text.replace(/\s+/g, " ").trim();
    stageCaption.textContent = clean.length > 240 ? `…${clean.slice(-240)}` : clean;
    stageCaption.classList.toggle("user", who === "user");
    bubbleSay(who, clean);
  }

  // ---- floating bubble (reading mode): what the tutor is saying, what you said, what it's looking up
  let bubbleTimer = null;
  let heardCaption = ""; // the reply so far, as heard
  let bubbleWho = "model";

  function bubbleSay(who, text) {
    bubbleWho = who;
    clearTimeout(bubbleTimer);
    bubbleText.textContent = text.length > 280 ? `…${text.slice(-280).replace(/^\S*\s/, "")}` : text;
    floatBubble.classList.toggle("user", who === "user");
    paintBubbleHead();
    if (text) floatBubble.classList.add("show");
  }

  function paintBubbleHead() {
    const activity = S.toolsPending.size ? stageActivity.textContent : "";
    const ic = activity ? (TOOL_LABELS[[...S.toolsPending.values()].pop()]?.({})[0] || "sparkles") : null;
    bubbleHead.replaceChildren(
      activity ? h("span", { class: "bubble-act" }, icon(ic, "sm"), activity)
        : h("b", {}, bubbleWho === "user" ? "You" : bubbleWho === "hint" ? "Voice Tutor" : "Tutor"));
    floatBubble.classList.toggle("busy", !!activity);
    if (activity) { clearTimeout(bubbleTimer); floatBubble.classList.add("show"); }
  }

  function hideBubbleSoon(ms = 7000) {
    clearTimeout(bubbleTimer);
    if (S.toolsPending.size || floatDock.matches(":hover")) return; // new speech cancels this anyway
    bubbleTimer = later(() => floatBubble.classList.remove("show"), ms);
  }

  function handleLive(msg) {
    switch (msg.type) {
      case "transcript":
        if (msg.role === "user") {
          if (!S.user) { tracker.newTurn(); resumeFollow(false); heardCaption = ""; } // you asked something: follow again
          if (S.model && S.model.text) finalizeModel();
          if (!S.user) S.user = addUser("", true);
          S.user.text += msg.text;
          S.user.bubble.textContent = S.user.text.trimStart();
          caption(S.user.text, "user");
        } else {
          finalizeUser();
          const m = ensureModel();
          m.text = joinText(m.text, msg.text);
          m.textEl.textContent = m.text.trimStart();
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
        const [ic, label] = (TOOL_LABELS[msg.name] || (() => ["sparkles", msg.name]))(msg.args || {});
        const act = h("div", { class: "act running" }, h("span", { class: "act-label" }, icon(ic), label));
        m.activityEl.append(act);
        m.acts.set(msg.id, act);
        stageActivity.textContent = label;
        S.toolsPending.set(msg.id, msg.name);
        floatBubble.classList.add("show");
        paintVoice();
        scrollToEnd();
        break;
      }
      case "tool_result": {
        S.toolsPending.delete(msg.id);
        const act = S.model?.acts.get(msg.id);
        if (act) {
          act.classList.remove("running");
          if (msg.error) { act.classList.add("error"); act.querySelector(".act-label").append(` — ${msg.error}`); }
          if (msg.ui) act.append(...refChips(msg.ui));
          if (msg.ui && S.model) S.model.refs.push(msg.ui);
        }
        if (msg.ui?.kind === "passage") onPassage(msg.ui);
        if (msg.ui?.kind === "search" || msg.ui?.kind === "passage") followMaterial(msg.ui);
        if (!S.toolsPending.size) stageActivity.textContent = msg.ui?.kind === "passage" ? `${msg.ui.section} · p. ${msg.ui.pages}` : "";
        paintVoice();
        break;
      }
      case "turn_complete":
        S.turnHasTools = false;
        atPlayback(() => { tracker.newTurn(); heardCaption = ""; hideBubbleSoon(); });
        finalizeTurn();
        refreshProgressSoon();
        break;
      case "interrupted":
        cancelPlaybackTimers(); // what was queued will never be heard
        S.turnHasTools = false;
        if (S.model?.text) S.model.textEl.classList.add("interrupted");
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

  // ================================================================ messages

  function addUser(text, isLive = false, ts = null) {
    const bubble = h("div", { class: "bubble" }, text);
    const el = h("div", { class: `msg user${isLive ? " live" : ""}`, title: ts ? clockTime(ts) : null }, bubble);
    removeEmpty();
    messagesInner.append(el);
    return { el, bubble, text };
  }

  function addModel(text = "", refs = [], isLive = false, ts = null) {
    const textEl = h("div", { class: "text" }, text);
    const activityEl = h("div", { class: "activity" });
    const copyBtn = h("button", { class: "icon-btn xs", "aria-label": "Copy reply", "data-tip": "Copy", onclick: () => copyText(textEl.textContent) }, icon("copy", "sm"));
    const el = h("div", { class: `msg model${isLive ? " live" : ""}` },
      h("div", { class: "avatar", "aria-hidden": "true" }, markIcon()),
      h("div", { class: "content" },
        h("div", { class: "msg-meta" }, h("b", {}, "Tutor"), ts ? h("span", {}, clockTime(ts)) : null, h("span", { class: "msg-actions" }, copyBtn)),
        activityEl, textEl));
    for (const ref of refs) {
      const act = h("div", { class: "act" }, h("span", { class: "act-label" }, ...refLabel(ref)));
      act.append(...refChips(ref));
      activityEl.append(act);
    }
    removeEmpty();
    messagesInner.append(el);
    return { el, textEl, activityEl, text, acts: new Map(), refs: [] };
  }

  function ensureModel() {
    if (!S.model) S.model = addModel("", [], true, new Date().toISOString());
    return S.model;
  }

  function finalizeUser() {
    if (!S.user) return;
    S.user.el.classList.remove("live");
    if (!S.user.text.trim()) S.user.el.remove();
    S.user = null;
  }

  function finalizeModel() {
    if (!S.model) return;
    S.model.el.classList.remove("live");
    S.model.el.querySelectorAll(".act.running").forEach((a) => a.classList.remove("running"));
    if (!S.model.text.trim() && !S.model.activityEl.children.length) S.model.el.remove();
    S.model = null;
  }

  function finalizeTurn() {
    finalizeUser();
    finalizeModel();
    S.toolsPending.clear();
    paintVoice();
  }

  function refLabel(ref) {
    if (ref.kind === "search") return [icon("search"), `Searched your sources for “${ref.query}”`];
    if (ref.kind === "web") return [icon("globe"), `Searched the web for “${ref.query}”`];
    if (ref.kind === "passage") return [icon("book"), "Read"];
    return [icon("sparkles"), ""];
  }

  function sourceTitle(docId) {
    return nb.sources.find((s) => s.doc_id === docId)?.title || "Source";
  }

  function refChips(ui) {
    if (ui.kind === "search") {
      const seen = new Set();
      return ui.refs.filter((r) => { const k = `${r.doc_id}:${r.page}`; if (seen.has(k)) return false; seen.add(k); return true; })
        .slice(0, 5).map((r) => docChip(r.doc_id, r.page, `p. ${r.pages}`, r.snippet, `${r.title} · ${r.section || ""}`));
    }
    if (ui.kind === "passage") {
      return [docChip(ui.doc_id, ui.page, `${ui.section} · p. ${ui.pages}`, ui.snippet, sourceTitle(ui.doc_id))];
    }
    if (ui.kind === "web") {
      return ui.links.slice(0, 5).map((l) => h("a", { class: "ref web", href: l.url, target: "_blank", rel: "noopener noreferrer", title: l.title },
        icon("globe", "sm"),
        h("span", {}, l.site || l.title)));
    }
    return [];
  }

  function docChip(docId, page, label, snippet, title) {
    const multi = nb.sources.length > 1;
    const n = nb.sources.findIndex((s) => s.doc_id === docId) + 1;
    return h("button", { class: "ref doc", title, onclick: () => { pauseFollow(); showPage(docId, page, snippet); revealViewer(); } },
      multi && n > 0 ? h("span", { class: "n" }, `${n}`) : icon("file", "sm"), h("span", {}, label));
  }

  function renderHistory() {
    messagesInner.replaceChildren();
    if (!nb.history.length) { messagesInner.append(emptyState()); return; }
    let lastDay = "";
    for (const entry of nb.history) {
      const day = entry.ts ? new Date(entry.ts).toDateString() : "";
      if (day && day !== lastDay) {
        lastDay = day;
        const label = day === new Date().toDateString() ? "Today" : new Date(entry.ts).toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });
        messagesInner.append(h("div", { class: "day-sep" }, h("span", {}, label)));
      }
      if (entry.role === "user") addUser(entry.text, false, entry.ts);
      else if (entry.role === "model") addModel(entry.text, entry.refs || [], false, entry.ts);
    }
    requestAnimationFrame(() => { messages.scrollTop = messages.scrollHeight; });
  }

  function emptyState() {
    const hasSources = nb.sources.length > 0;
    const started = nb.sources.some((s) => s.position?.started);
    const ideas = hasSources
      ? [[started ? "play" : "play", started ? "Continue where we left off" : "Teach me from the beginning"],
         ["info", "What is this about?"], ["sparkles", "Quiz me on the first chapter"], ["globe", "What's the latest on this topic?"]]
      : [];
    return h("div", { class: "chat-empty", dataset: { empty: "" } },
      h("div", { class: "empty-mark" }, markIcon()),
      h("h2", {}, hasSources ? "Ready when you are" : "Add a source to begin"),
      h("p", {}, hasSources
        ? "Tap the orb and talk, or type below. I'll teach, explain, research and brainstorm, and always tell you what comes from your sources and what comes from the web."
        : "Drop a PDF anywhere on this page, or use Add. It's indexed on your computer, then we can talk about it."),
      hasSources
        ? h("div", { class: "suggestions" }, ideas.map(([ic, t]) => h("button", { class: "suggestion", onclick: () => sendText(t) }, icon(ic, "sm"), t)))
        : h("button", { class: "btn primary lg", onclick: pickFiles }, icon("upload"), "Add a PDF"));
  }

  function removeEmpty() {
    messagesInner.querySelector("[data-empty]")?.remove();
  }

  function scrollToEnd(force = false) {
    const near = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 160;
    if (near || force) messages.scrollTop = messages.scrollHeight;
  }

  function renderQuick() {
    const active = nb.sources.find((s) => s.doc_id === nb.active_source);
    const started = active?.position?.started;
    const items = !nb.sources.length ? [] : QUICK.filter(([key]) => (started ? key !== "start" : key !== "continue"));
    quick.replaceChildren(...items.map(([, ic, label, text]) => h("button", { class: "suggestion", onclick: () => sendText(text) }, icon(ic, "sm"), label)));
    quick.hidden = !items.length;
  }

  async function sendText(text) {
    if (!text.trim()) return;
    tracker.newTurn();
    resumeFollow(false);
    if (!(await ensureReady())) return;
    finalizeTurn();
    addUser(text, false, new Date().toISOString());
    scrollToEnd(true);
    await live.sendText(text);
  }

  function sendTyped() {
    const text = textarea.value.trim();
    if (!text) return;
    textarea.value = "";
    autosize();
    sendBtn.disabled = true;
    sendText(text);
  }

  function autosize() {
    textarea.style.height = "auto";
    if (!textarea.value) { textarea.style.height = ""; textarea.style.overflowY = "hidden"; return; }
    textarea.style.height = `${Math.min(textarea.scrollHeight, 140)}px`;
    textarea.style.overflowY = textarea.scrollHeight > 140 ? "auto" : "hidden";
  }

  async function clearChat() {
    if (!nb.history.length && !messagesInner.querySelector(".msg")) return;
    const ok = await confirmDialog({ title: "Clear the conversation?", body: "This removes the chat history for this notebook. Your sources and reading progress stay.", confirm: "Clear" });
    if (!ok) return;
    try {
      await api.clearHistory(id);
      nb.history = [];
      S.user = S.model = null;
      renderHistory();
      toast("Conversation cleared", "success");
    } catch (e) { toast(e.message, "error"); }
  }

  // ================================================================ sources & outline

  function paintHeader() {
    const pages = nb.sources.reduce((n, s) => n + (s.pages || 0), 0);
    nbSub.textContent = nb.sources.length
      ? `${nb.sources.length} source${nb.sources.length > 1 ? "s" : ""} · ${pages} pages`
      : "No sources yet";
    sourceCount.textContent = nb.sources.length ? String(nb.sources.length) : "";
  }

  function renderSources() {
    paintHeader();
    const items = nb.sources.map((s, i) => {
      const active = s.doc_id === nb.active_source;
      const remove = h("button", { class: "icon-btn xs remove", "aria-label": `Remove ${s.title}`, "data-tip": "Remove",
        onclick: async (e) => {
          e.stopPropagation();
          const ok = await confirmDialog({ title: `Remove “${s.title}”?`, body: "It will be removed from this notebook. If no other notebook uses it, its index and reading progress are deleted too.", confirm: "Remove" });
          if (!ok) return;
          try { await api.removeSource(id, s.doc_id); await reload(); toast("Source removed", "success"); } catch (err) { toast(err.message, "error"); }
        } }, icon("trash", "sm"));
      return h("div", { class: `source${active ? " active" : ""}`, role: "button", tabindex: "0",
        title: active ? "This source is being taught" : "Teach this source",
        onclick: () => setActive(s.doc_id), onkeydown: (e) => { if (e.key === "Enter") setActive(s.doc_id); } },
        h("div", { class: "file-tile" }, icon("file"), nb.sources.length > 1 ? h("i", {}, String(i + 1)) : null),
        h("div", { class: "source-text" },
          h("div", { class: "title" }, s.title),
          h("div", { class: "sub" }, s.missing ? "Missing — add it again" : `${s.pages} pages`,
            active ? h("span", { class: "badge" }, "Teaching") : null)),
        s.missing ? null : ring(s.covered, 30, 3),
        remove);
    });
    const jobs = [...S.jobs.values()].map((j) => {
      const failed = j.status === "error";
      return h("div", { class: `source job${failed ? " error" : ""}` },
        h("div", { class: "file-tile" }, icon(failed ? "alert" : "loader", failed ? "" : "spin")),
        h("div", { class: "source-text" }, h("div", { class: "title" }, j.file_name),
          h("div", { class: "sub" }, failed ? j.error : jobStageText(j)),
          failed ? null : h("div", { class: `job-bar${j.stage === "queued" || j.stage === "parsing" ? " indeterminate" : ""}` }, h("i", { style: { width: `${j.percent}%` } }))),
        failed ? h("button", { class: "icon-btn xs", "aria-label": "Dismiss", onclick: async () => { await api.dismissJob(j.id).catch(() => {}); S.jobs.delete(j.id); renderSources(); } }, icon("x", "sm")) : null);
    });
    sourcesList.replaceChildren(...items, ...jobs, ...(nb.sources.length || S.jobs.size ? [] : [
      h("button", { class: "add-source", onclick: pickFiles },
        h("span", { class: "add-ic" }, icon("upload")),
        h("span", {}, h("b", {}, "Add a PDF"), "Click, or drop files anywhere"))]));
    renderQuick();
  }

  function jobStageText(j) {
    if (j.stage === "queued") return "Waiting…";
    if (j.stage === "parsing") return "Reading the PDF…";
    if (j.stage === "chunking") return "Finding sections…";
    if (j.stage === "embedding") return `Indexing ${j.done} of ${j.total}`;
    if (j.stage === "saving") return "Saving…";
    return "Done";
  }

  async function setActive(docId) {
    if (docId === nb.active_source) { pauseFollow(); showPage(docId, currentPageFor(docId)); revealViewer(); return; }
    try {
      await api.updateNotebook(id, { active_source: docId });
      nb.active_source = docId;
      renderSources();
      await loadOutline();
      showPage(docId, currentPageFor(docId));
    } catch (e) { toast(e.message, "error"); }
  }

  function currentPageFor(docId) {
    const s = nb.sources.find((x) => x.doc_id === docId);
    const pages = s?.position?.pages;
    return pages ? parseInt(String(pages).split("-")[0], 10) || 1 : 1;
  }

  async function loadOutline() {
    const docId = nb.active_source;
    if (!docId) { outlineBox.replaceChildren(); S.outline = null; return; }
    try { S.outline = await api.outline(docId); }
    catch (e) { outlineBox.replaceChildren(h("div", { class: "viewer-empty" }, e.message)); return; }
    renderOutline();
  }

  function renderOutline() {
    const o = S.outline;
    if (!o) return;
    const current = o.position?.started ? o.position.section_id : null;
    const items = o.sections.map((sec) => h("button", {
      class: `outline-item l${Math.min(sec.level, 3)}${sec.covered ? " covered" : ""}${sec.id === current ? " current" : ""}`,
      title: `Teach “${sec.title}”`,
      onclick: () => teachSection(sec),
    }, h("span", { class: "node" }, sec.covered ? icon("check", "sm") : null), h("span", { class: "t" }, sec.title), h("span", { class: "p" }, sec.pages)));
    const menuBtn = h("button", { class: "icon-btn xs", "aria-label": "Outline options", onclick: (e) => openMenu(e.currentTarget, [
      { label: "Start from the beginning", icon: "play", action: () => sendText("Teach me from the beginning.") },
      { label: "Reset reading progress", icon: "reset", action: resetProgress },
    ]) }, icon("more", "sm"));
    outlineBox.replaceChildren(
      h("div", { class: "outline-head" },
        h("h3", {}, "Outline"),
        h("span", { class: "outline-pct" }, ring(parseInt(o.document_covered, 10), 16, 2.5), `${o.document_covered} covered`),
        menuBtn),
      h("div", { class: "outline" }, items.length ? items : h("div", { class: "viewer-empty" }, "No sections found.")));
  }

  async function resetProgress() {
    const o = S.outline;
    const ok = await confirmDialog({ title: "Reset reading progress?", body: `Start “${o?.title || "this source"}” from scratch. Covered sections will be unmarked.`, confirm: "Reset", danger: false });
    if (!ok) return;
    try { S.outline = await api.resetProgress(nb.active_source); renderOutline(); await reloadSources(); toast("Progress reset", "success"); } catch (e) { toast(e.message, "error"); }
  }

  function teachSection(sec) {
    resumeFollow(false);
    showPage(nb.active_source, parseInt(sec.pages, 10) || 1);
    const n = nb.sources.findIndex((s) => s.doc_id === nb.active_source) + 1;
    const where = nb.sources.length > 1 ? ` of source ${n}` : "";
    sendText(`Teach me the section “${sec.title}” (section id ${sec.id})${where}.`);
  }

  let progressTimer = null;
  function refreshProgressSoon() {
    clearTimeout(progressTimer);
    progressTimer = later(async () => { await reloadSources(); if (nb.active_source) await loadOutline(); }, 600);
  }

  async function reloadSources() {
    try {
      const fresh = await api.notebook(id);
      nb.sources = fresh.sources;
      nb.active_source = fresh.active_source;
      renderSources();
    } catch { /* transient */ }
  }

  async function reload() {
    nb = await api.notebook(id);
    renderSources();
    await loadOutline();
    if (!nb.sources.some((s) => s.doc_id === S.viewer.docId)) {
      S.viewer.docId = null;
      if (nb.active_source) showPage(nb.active_source, currentPageFor(nb.active_source)); else renderViewerEmpty();
    }
    if (!messagesInner.querySelector(".msg")) renderHistory();
  }

  // ================================================================ uploads

  async function upload(files) {
    const pdfs = files.filter((f) => f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf"));
    if (!pdfs.length) { toast("Only PDF files can be added.", "error"); return; }
    if (pdfs.length < files.length) toast("Skipped files that aren't PDFs.", "info");
    try {
      const { jobs } = await api.upload(id, pdfs);
      for (const j of jobs) S.jobs.set(j.id, j);
      renderSources();
      pollJobs();
    } catch (e) { toast(e.message, "error", 6000); }
  }

  let polling = false;
  async function pollJobs() {
    if (polling || S.disposed) return;
    polling = true;
    try {
      while (!S.disposed && [...S.jobs.values()].some((j) => j.status === "queued" || j.status === "running")) {
        await new Promise((r) => later(r, 700));
        for (const j of [...S.jobs.values()]) {
          if (j.status !== "queued" && j.status !== "running") continue;
          try {
            const fresh = await api.job(j.id);
            S.jobs.set(j.id, fresh);
            if (fresh.status === "done") {
              S.jobs.delete(j.id);
              api.dismissJob(j.id).catch(() => {});
              toast(`Added “${fresh.title || fresh.file_name}”`, "success");
              await reload();
            }
          } catch { S.jobs.delete(j.id); }
        }
        renderSources();
      }
    } finally { polling = false; }
  }

  let dragDepth = 0;
  const onDragEnter = (e) => { if (![...(e.dataTransfer?.types || [])].includes("Files")) return; e.preventDefault(); dragDepth++; drop.classList.add("show"); };
  const onDragOver = (e) => { if ([...(e.dataTransfer?.types || [])].includes("Files")) e.preventDefault(); };
  const onDragLeave = () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) drop.classList.remove("show"); };
  const onDrop = (e) => {
    if (!e.dataTransfer?.files?.length) return;
    e.preventDefault();
    dragDepth = 0;
    drop.classList.remove("show");
    upload([...e.dataTransfer.files]);
  };
  window.addEventListener("dragenter", onDragEnter);
  window.addEventListener("dragover", onDragOver);
  window.addEventListener("dragleave", onDragLeave);
  window.addEventListener("drop", onDrop);

  // ================================================================ viewer

  function renderViewerEmpty() {
    S.viewer.docId = null;
    docName.textContent = "Page viewer";
    docName.removeAttribute("title");
    openPdfBtn.removeAttribute("href");
    pageInput.value = "";
    pageTotal.textContent = "";
    pdf.empty(h("div", { class: "viewer-empty" }, h("span", { class: "empty-ic" }, icon("file")),
      h("b", {}, "No page yet"), h("span", {}, "Pages appear here as we go through your sources.")));
  }

  // Scroll the viewer to a page of a source (opening that source first if needed) and highlight the
  // passage being discussed.
  let showToken = 0;
  async function showPage(docId, page, snippet = null) {
    if (!docId) return;
    const src = nb.sources.find((s) => s.doc_id === docId);
    if (!src || src.missing) return;
    const token = ++showToken;
    S.viewer.highlight = snippet;
    if (docId !== S.viewer.docId || !pdf.total) {
      S.viewer.docId = docId;
      docName.textContent = src.title;
      docName.title = src.title;
      if (!(await pdf.open(docId, src.pages || 1)) || token !== showToken) return;
    }
    page = Math.min(Math.max(1, page || 1), pdf.total);
    pdf.clearHighlight();
    pdf.goTo(page);
    if (!snippet) return;
    try {
      const sentence = snippet.split(/(?<=[.!?])\s+/).find((x) => x.split(" ").length >= 5) || snippet;
      const loc = await api.locate(docId, sentence, page, page + 1);
      if (token !== showToken || S.viewer.docId !== docId || !loc.page) return;
      pdf.highlight(loc.page, loc.rects);
    } catch { /* highlight is best-effort */ }
  }

  function onPassage(ui) {
    if (!ui.text && S.follow && !S.followPaused) showPage(ui.doc_id, ui.page, ui.snippet); // older server: no text to follow
    if (ui.doc_id !== nb.active_source) { nb.active_source = ui.doc_id; renderSources(); }
    refreshProgressSoon();
  }

  // ================================================================ follow along
  // The page follows what the tutor is *saying*: tool results bring in the passages it's working from, the
  // tracker (follow.js) matches its speech to their sentences, and each step is timed to when that audio is
  // actually heard. Scrolling or jumping yourself pauses following (a pill brings you back); asking
  // something new resumes it.

  const tracker = new FollowTracker();
  const playbackTimers = new Set();

  function atPlayback(fn) {
    const lag = live.playbackLag * 1000;
    if (lag < 40) { fn(); return; }
    const t = setTimeout(() => { playbackTimers.delete(t); if (!S.disposed) fn(); }, lag);
    playbackTimers.add(t);
  }

  function cancelPlaybackTimers() {
    playbackTimers.forEach(clearTimeout);
    playbackTimers.clear();
  }

  const lastPage = (pages, first) => parseInt(String(pages || "").split("-").pop(), 10) || first;

  function followMaterial(ui) {
    const cands = ui.kind === "passage"
      ? [{ doc_id: ui.doc_id, first: ui.page, last: lastPage(ui.pages, ui.page), text: ui.text, boost: 1.15 }]
      : (ui.refs || []).map((r) => ({ doc_id: r.doc_id, first: r.page, last: lastPage(r.pages, r.page), text: r.text }));
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

  let retryTimer = null;
  function hearSpeech(piece) {
    focusSentence(tracker.hear(piece));
    clearTimeout(retryTimer);
    if (tracker.retryAt) retryTimer = later(() => hearSpeech(""), Math.max(30, tracker.retryAt - performance.now() + 20));
  }

  let focusToken = 0;
  async function focusSentence(seg) {
    if (!seg || !S.follow) return;
    const token = ++focusToken;
    let loc;
    try { loc = await api.locate(seg.doc_id, seg.text, seg.first, seg.last); } catch { return; }
    if (token !== focusToken || S.disposed || !loc.page) return; // not found on the page: keep the current spot
    const same = S.focus && S.focus.doc_id === seg.doc_id && S.focus.page === loc.page
      && JSON.stringify(S.focus.rects) === JSON.stringify(loc.rects);
    if (same && S.viewer.docId === seg.doc_id) return; // same line (overlapping hits): no flicker
    S.focus = { doc_id: seg.doc_id, page: loc.page, rects: loc.rects };
    await showFocus(!S.followPaused);
    paintFollowPill();
  }

  // move=false: repaint the highlight where it is without scrolling (following is paused).
  async function showFocus(move) {
    const f = S.focus;
    if (!f) return;
    const src = nb.sources.find((x) => x.doc_id === f.doc_id);
    if (!src || src.missing) return;
    if (f.doc_id !== S.viewer.docId) {
      if (!move) return;
      ++showToken;
      S.viewer.docId = f.doc_id;
      docName.textContent = src.title;
      docName.title = src.title;
      if (!(await pdf.open(f.doc_id, src.pages || 1)) || S.focus !== f) return;
    }
    pdf.highlight(f.page, f.rects, move ? "reveal" : false);
  }

  function pauseFollow() {
    if (!S.follow || S.followPaused) return;
    S.followPaused = true;
    paintFollowPill();
  }

  function resumeFollow(jump) {
    const was = S.followPaused;
    S.followPaused = false;
    paintFollowPill();
    if (jump || (was && live.speaking)) showFocus(true);
  }

  function setFollow(on) {
    S.follow = on;
    S.followPaused = false;
    paintFollowPill();
    if (on) showFocus(true);
  }

  function paintFollowPill() {
    const f = S.focus;
    followPill.hidden = !(S.follow && S.followPaused && f && live.active); // only matters mid-conversation
    if (followPill.hidden) return;
    const multi = nb.sources.length > 1;
    const n = nb.sources.findIndex((x) => x.doc_id === f.doc_id) + 1;
    followPillText.textContent = `Back to the tutor · ${multi && n ? `source ${n}, ` : ""}p. ${f.page}`;
  }

  function toggleViewer() {
    if (S.reading) { setReading(false); return; }
    if (window.matchMedia("(max-width: 760px)").matches) { setTab(S.tab === "page" ? "chat" : "page"); return; }
    if (window.matchMedia("(max-width: 980px)").matches) body.classList.toggle("viewer-open");
    else body.classList.toggle("viewer-hidden");
    paintViewerToggle();
    applySplit();
  }

  function revealViewer() {
    if (S.reading) return;
    if (window.matchMedia("(max-width: 760px)").matches) setTab("page");
    else if (window.matchMedia("(max-width: 980px)").matches) body.classList.add("viewer-open");
    else body.classList.remove("viewer-hidden");
    paintViewerToggle();
    applySplit();
  }

  function paintViewerToggle() {
    const narrow = window.matchMedia("(max-width: 980px)").matches;
    viewerToggle.classList.toggle("active", narrow ? body.classList.contains("viewer-open") : !body.classList.contains("viewer-hidden"));
  }

  function setTab(tab) {
    S.tab = tab;
    body.dataset.tab = tab;
    tabs.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  }

  // ================================================================ resizable splits
  // Sources width is kept in px; the viewer's as a share of the width, so it scales with the window.
  // The conversation always keeps at least CHAT_MIN. Dragging the sources edge far left collapses it.

  const SPLIT_KEY = "vt-split";
  const SOURCES = { min: 220, max: 440, def: 280, collapseBelow: 150 };
  const VIEWER = { min: 320, def: 0.46 };
  const CHAT_MIN = 340;
  const GUTTER = 12;
  const split = { sources: SOURCES.def, viewer: VIEWER.def, collapsed: false };
  try {
    const saved = JSON.parse(localStorage.getItem(SPLIT_KEY) || "{}");
    if (Number.isFinite(saved.sources)) split.sources = saved.sources;
    if (Number.isFinite(saved.viewer) && saved.viewer > 0 && saved.viewer < 1) split.viewer = saved.viewer;
    split.collapsed = saved.collapsed === true;
  } catch { /* storage blocked */ }
  const sized = { sources: split.sources, viewer: 0, room: 0 };

  function saveSplit() {
    try { localStorage.setItem(SPLIT_KEY, JSON.stringify(split)); } catch { /* storage blocked */ }
  }

  function applySplit() {
    if (window.matchMedia("(max-width: 760px)").matches) return;
    const viewerInGrid = !body.classList.contains("viewer-hidden") && window.matchMedia("(min-width: 981px)").matches;
    const room = body.clientWidth - 24 - GUTTER * (viewerInGrid ? 2 : 1); // minus padding and gutters
    const sourcesRoom = Math.max(SOURCES.min, room - CHAT_MIN - (viewerInGrid ? VIEWER.min : 0));
    const sources = split.collapsed ? 0 : Math.round(Math.min(Math.max(split.sources, SOURCES.min), SOURCES.max, sourcesRoom));
    let viewer = 0;
    if (viewerInGrid) viewer = Math.round(Math.max(VIEWER.min, Math.min(split.viewer * room, room - sources - CHAT_MIN)));
    Object.assign(sized, { sources, viewer, room });
    body.classList.toggle("sources-collapsed", split.collapsed);
    body.style.setProperty("--w-sources", `${sources}px`);
    body.style.setProperty("--w-viewer", `${viewer}px`);
    gutterSources.setAttribute("aria-valuenow", String(sources));
    gutterViewer.setAttribute("aria-valuenow", String(viewer));
  }

  function setSources(px) {
    if (px < SOURCES.collapseBelow) split.collapsed = true;
    else { split.collapsed = false; split.sources = Math.min(Math.max(px, SOURCES.min), SOURCES.max); }
    applySplit();
  }

  function setViewer(px) {
    if (!sized.room) return;
    split.viewer = Math.min(0.9, Math.max(0.1, px / sized.room));
    applySplit();
    split.viewer = sized.viewer / sized.room; // remember what actually fits, not how far the mouse went
  }

  function resetSplit(which) {
    if (which === "sources") { split.sources = SOURCES.def; split.collapsed = false; } else split.viewer = VIEWER.def;
    applySplit();
    saveSplit();
  }

  function makeDraggable(handle, which) {
    handle.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      try { handle.setPointerCapture(e.pointerId); } catch { /* pointer already gone */ }
      const startX = e.clientX;
      const start = which === "sources" ? sized.sources : sized.viewer;
      let moved = false;
      nbEl.classList.add("resizing");
      handle.classList.add("dragging");
      const move = (ev) => {
        const dx = ev.clientX - startX;
        if (!moved && Math.abs(dx) < 3) return;
        moved = true;
        if (which === "sources") setSources(start + dx); else setViewer(start - dx);
      };
      const up = () => {
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", up);
        handle.removeEventListener("pointercancel", up);
        nbEl.classList.remove("resizing");
        handle.classList.remove("dragging");
        if (!moved && which === "sources" && split.collapsed) { split.collapsed = false; applySplit(); } // click to reopen
        saveSplit();
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", up);
      handle.addEventListener("pointercancel", up);
    });
    handle.addEventListener("dblclick", () => resetSplit(which));
    handle.addEventListener("keydown", (e) => {
      const step = e.shiftKey ? 64 : 24;
      const dir = e.key === "ArrowLeft" ? -1 : e.key === "ArrowRight" ? 1 : 0;
      if (dir) {
        e.preventDefault();
        e.stopPropagation();
        if (which === "sources") setSources((split.collapsed ? SOURCES.min - step : sized.sources) + dir * step);
        else setViewer(sized.viewer - dir * step);
        saveSplit();
      } else if (e.key === "Enter" && which === "sources") {
        e.preventDefault();
        split.collapsed = !split.collapsed;
        applySplit();
        saveSplit();
      }
    });
  }
  makeDraggable(gutterSources, "sources");
  makeDraggable(gutterViewer, "viewer");
  const bodyResize = new ResizeObserver(() => { applySplit(); paintViewerToggle(); });
  bodyResize.observe(body);

  // ================================================================ full-screen reading mode
  // The PDF fills the screen (real browser full screen when allowed) and scrolls freely; the tutor
  // floats at the side as the voice orb, with a bubble showing what it says.

  function setReading(on) {
    if (on === S.reading) return;
    S.reading = on;
    nbEl.classList.toggle("reading", on);
    fullBtn.replaceChildren(icon(on ? "minimize" : "maximize", "sm"));
    fullBtn.setAttribute("aria-label", on ? "Exit full screen (F)" : "Full-screen reading (F)");
    fullBtn.dataset.tip = on ? "Exit full screen (F)" : "Full screen (F)";
    dockOrb.setPaused(on);
    stageOrb?.setPaused(on);
    if (on) {
      // requestFullscreen needs the click/keypress that got us here, so it comes before anything async.
      if (!document.fullscreenElement && document.documentElement.requestFullscreen) {
        document.documentElement.requestFullscreen({ navigationUI: "hide" }).catch(() => { /* not allowed: fill the window instead */ });
      }
      floatOrb = new FluidOrb({ size: 60, state: S.voiceKey });
      floatOrbBtn.prepend(floatOrb.el);
      paintVoice();
      if (!live.active && !nb.history.length) {
        bubbleSay("hint", "Tap me to talk about what you're reading. I'll follow along on the page.");
        hideBubbleSoon(6000);
      }
      if (!S.viewer.docId && nb.active_source) showPage(nb.active_source, currentPageFor(nb.active_source));
      later(() => pdf.focus(), 60);
      wakeChrome();
    } else {
      wakeChrome();
      floatOrb?.destroy();
      floatOrb = null;
      floatOrbBtn.querySelector(".fluid-orb")?.remove();
      toggleComposer(false);
      floatBubble.classList.remove("show");
      if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
      applySplit();
    }
  }

  // The floating toolbars fade out while you read and come back when the mouse moves.
  let idleTimer = null;
  function wakeChrome() {
    nbEl.classList.remove("idle");
    clearTimeout(idleTimer);
    if (!S.reading) return;
    idleTimer = later(() => {
      const busy = viewerPanel.querySelector(".viewer-head:hover, .viewer-foot:hover, .viewer-head:focus-within, .viewer-foot:focus-within");
      if (S.reading && !busy) nbEl.classList.add("idle"); else wakeChrome();
    }, 2600);
  }
  for (const type of ["pointermove", "pointerdown"]) viewerPanel.addEventListener(type, () => { if (S.reading) wakeChrome(); });

  // Leaving full screen with the browser's own Esc / F11 also leaves reading mode.
  const onFullscreen = () => { if (!document.fullscreenElement && S.reading) setReading(false); };
  document.addEventListener("fullscreenchange", onFullscreen);

  function toggleComposer(open = floatComposer.hidden) {
    floatComposer.hidden = !open;
    floatType.classList.toggle("on", open);
    if (open) floatInput.focus();
    else if (S.reading) pdf.focus();
  }

  function sendFloat() {
    const text = floatInput.value.trim();
    if (!text) return;
    floatInput.value = "";
    fitFloatInput();
    floatSend.disabled = true;
    bubbleSay("user", text);
    sendText(text);
  }

  async function pickIcon() {
    const choice = await modal((close) => [
      h("h2", {}, "Choose an icon"),
      h("p", {}, "Shown on the notebook card and in the header."),
      h("div", { class: "icon-grid" }, NOTEBOOK_ICONS.map((name) =>
        h("button", { class: name === nb.icon ? "on" : "", onclick: () => close(name), "aria-label": name }, notebookIcon(name)))),
    ]);
    if (!choice) return;
    try {
      await api.updateNotebook(id, { icon: choice });
      nb.icon = choice;
      iconBtn.replaceChildren(notebookIcon(choice));
    } catch (e) { toast(e.message, "error"); }
  }


  // ================================================================ keyboard & events

  const typing = (el) => el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable);
  const onKey = (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey || document.querySelector(".overlay")) return;
    if (typing(document.activeElement)) return;
    if (e.key === "/") { e.preventDefault(); if (S.reading) toggleComposer(true); else textarea.focus(); }
    else if ((e.key === "m" || e.key === "M") && live.active) { e.preventDefault(); live.toggleMic(); }
    else if ((e.key === "f" || e.key === "F") && (S.viewer.docId || S.reading)) { e.preventDefault(); setReading(!S.reading); }
    else if (e.key === "Escape" && S.reading) { e.preventDefault(); setReading(false); }
    else if (e.key === "Escape" && live.active) { e.preventDefault(); live.end(); }
    else if (e.key === "ArrowLeft" && pdf.total) { e.preventDefault(); pauseFollow(); pdf.prev(); }
    else if (e.key === "ArrowRight" && pdf.total) { e.preventDefault(); pauseFollow(); pdf.next(); }
  };
  document.addEventListener("keydown", onKey);

  const onSettings = (e) => {
    if (live.active) toast(`End and restart the conversation to hear ${e.detail.voice}.`, "info", 5000);
  };
  window.addEventListener("vt-settings", onSettings);

  // ================================================================ boot

  for (const j of nb.jobs || []) S.jobs.set(j.id, j);
  renderSources();
  renderHistory();
  applySplit();
  paintVoice();
  if (!shared.status?.gemini_key) {
    banner.replaceChildren(h("div", { class: "banner" }, icon("key"),
      h("div", {}, h("b", {}, "Voice needs a Gemini API key. "), "Get one free at aistudio.google.com/apikey, add ",
        h("code", {}, "GEMINI_API_KEY=…"), " to the ", h("code", {}, ".env"), " file in the project folder, then restart run.bat.")));
  }
  await loadOutline();
  if (nb.active_source) showPage(nb.active_source, currentPageFor(nb.active_source)); else renderViewerEmpty();
  if (S.jobs.size) pollJobs();

  return () => {
    S.disposed = true;
    cancelPlaybackTimers();
    if (S.reading && document.fullscreenElement) document.exitFullscreen().catch(() => {});
    live.end();
    dockOrb.destroy();
    stageOrb?.destroy();
    floatOrb?.destroy();
    pdf.destroy();
    stageResize.disconnect();
    bodyResize.disconnect();
    document.removeEventListener("fullscreenchange", onFullscreen);
    timers.forEach(clearTimeout);
    clearTimeout(progressTimer);
    window.removeEventListener("dragenter", onDragEnter);
    window.removeEventListener("dragover", onDragOver);
    window.removeEventListener("dragleave", onDragLeave);
    window.removeEventListener("drop", onDrop);
    window.removeEventListener("vt-settings", onSettings);
    document.removeEventListener("keydown", onKey);
  };
}
