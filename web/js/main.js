// App shell: hash router (#/ home, #/nb/<id> notebook), shared top-bar pieces, settings sheet.
import { api } from "./api.js";
import { renderHome } from "./home.js";
import { renderNotebook } from "./notebook.js";
import { getTheme, h, icon, setTheme, sheet, toast } from "./ui.js";

const app = document.getElementById("app");
let cleanup = null;
export const shared = { status: null };

export async function refreshStatus() {
  try {
    shared.status = await api.status();
  } catch (e) {
    shared.status = null;
  }
  document.querySelectorAll("[data-status-pill]").forEach((el) => el.replaceWith(statusPill()));
  return shared.status;
}

// Shown only when something needs attention; otherwise an empty placeholder (keeps the top bar quiet).
export function statusPill() {
  const s = shared.status;
  let cls = "", text = "";
  if (!s) { cls = "bad"; text = "Server offline"; }
  else if (!s.gemini_key) { cls = "warn"; text = "Add Gemini key"; }
  else if (s.embedder === "loading") { cls = "warn"; text = "Warming up"; }
  else if (s.embedder === "error") { cls = "bad"; text = "Model error"; }
  const pill = text
    ? h("button", { class: `pill ${cls} status-pill`, title: "Open settings", onclick: () => openSettings() }, h("span", { class: "dot" }), text)
    : h("span", { hidden: true });
  pill.dataset.statusPill = "";
  return pill;
}

const VOICE_NOTES = {
  Kore: "Firm and clear", Puck: "Upbeat and lively", Charon: "Calm and informative", Zephyr: "Bright and friendly",
  Fenrir: "Energetic", Leda: "Youthful and light", Orus: "Deep and steady", Aoede: "Warm and breezy",
};

// One preview player shared by all voice cards: only one sample plays at a time.
const preview = { audio: null, voice: null, onChange: null };

function stopPreview() {
  if (preview.audio) { preview.audio.pause(); preview.audio = null; }
  preview.voice = null;
  preview.onChange?.();
}

async function playPreview(voice, setLoading) {
  if (preview.voice === voice) { stopPreview(); return; }
  stopPreview();
  preview.voice = voice;
  setLoading(true);
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
    setLoading(false);
    preview.onChange?.();
  }
}

let settingsOpen = false;

export async function openSettings() {
  if (settingsOpen) return;
  settingsOpen = true;
  // Open immediately with what we know; refresh in the background (never block on the server).
  let s = shared.status;
  const pending = refreshStatus();
  try {
    await sheet((close) => {
      let chosen = s?.voice || "Kore";
      let loadingVoice = null;
      const body = h("div", { class: "sheet-body scroll" });
      const saveBtn = h("button", { class: "btn primary", disabled: true }, "Save changes");

      const voiceCard = (v) => {
        const playing = preview.voice === v && !loadingVoice;
        const playBtn = h("button", {
          class: `voice-play${playing ? " playing" : ""}`, "aria-label": playing ? `Stop ${v} preview` : `Preview ${v}`,
          onclick: (e) => {
            e.stopPropagation();
            playPreview(v, (on) => { loadingVoice = on ? v : null; render(); });
          },
        }, icon(loadingVoice === v ? "loader" : playing ? "pause" : "play", "sm"));
        return h("div", {
          class: `voice-opt${v === chosen ? " selected" : ""}`, role: "radio", tabindex: "0", "aria-checked": String(v === chosen),
          onclick: () => { chosen = v; render(); },
          onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); chosen = v; render(); } },
        }, playBtn, h("span", { class: "vtext" }, h("b", {}, v), h("small", {}, VOICE_NOTES[v] || "")),
        h("span", { class: "vcheck" }, icon("check", "sm")));
      };

      const render = () => {
        const voices = s?.voices || ["Kore", "Puck", "Charon", "Zephyr", "Fenrir", "Leda", "Orus", "Aoede"];
        saveBtn.disabled = !(s && chosen !== s.voice);
        body.replaceChildren(...[ // filter: replaceChildren would print null as text
          !s ? h("div", { class: "banner error" }, icon("alert"), h("div", {}, "Can't reach the server right now. Is the “Voice Tutor - Server” window open?")) : null,
          s && !s.gemini_key ? h("div", { class: "banner" }, icon("key"),
            h("div", {}, h("b", {}, "Gemini API key missing. "), "Get a free key at aistudio.google.com/apikey, paste it into ",
              h("code", {}, ".env"), " after ", h("code", {}, "GEMINI_API_KEY="), " and restart run.bat.")) : null,

          section("Voice", "Press play to hear a voice. The first preview of each takes a few seconds.",
            h("div", { class: "voice-grid", role: "radiogroup", "aria-label": "Voice" }, voices.map(voiceCard))),

          section("Appearance", null, themeSwitch()),

          section("System", null, h("div", { class: "status-list" },
            statusRow("wave", "Voice model", s?.live_model || "—", s ? "ok" : "bad"),
            statusRow("key", "Gemini key", s?.gemini_key ? "Configured" : "Missing", s?.gemini_key ? "ok" : "warn"),
            statusRow("globe", "Web search", s?.web_provider === "tavily" ? "Tavily" : "DuckDuckGo · add TAVILY_API_KEY for better results",
              s?.web_provider === "tavily" ? "ok" : "neutral"),
            statusRow("cpu", "Embeddings", s?.embedder === "ready" ? "Ready · runs locally" : (s?.embedder || "—"),
              s?.embedder === "ready" ? "ok" : s?.embedder === "error" ? "bad" : "warn"),
            s?.log_file ? statusRow("logs", "Logs", s.log_file, "neutral") : null)),

          section("Keyboard shortcuts", null, h("div", { class: "shortcuts" },
            shortcut(["/"], "Type a message"),
            shortcut(["M"], "Mute or unmute the mic during a conversation"),
            shortcut(["Esc"], "End the conversation, or close a dialog"),
            shortcut(["←", "→"], "Previous / next page in the viewer"),
            shortcut(["F"], "Full-screen reading with the floating tutor"),
            shortcut(["Ctrl", "Scroll"], "Zoom the page"))),
        ].filter(Boolean));
      };
      preview.onChange = () => { if (body.isConnected) render(); };

      saveBtn.addEventListener("click", async () => {
        saveBtn.disabled = true;
        saveBtn.textContent = "Saving…";
        try {
          await api.saveSettings({ voice: chosen });
          await refreshStatus();
          toast(`Voice set to ${chosen}`, "success");
          window.dispatchEvent(new CustomEvent("vt-settings", { detail: { voice: chosen } }));
          close(true);
        } catch (e) {
          toast(e.message, "error", 6000);
          saveBtn.textContent = "Save changes";
          saveBtn.disabled = false;
        }
      });

      render();
      pending.then((fresh) => {
        if (fresh) { const keep = chosen !== s?.voice ? chosen : fresh.voice; s = fresh; chosen = keep; }
        if (body.isConnected) render();
      });

      return [
        h("div", { class: "sheet-head" },
          h("div", {}, h("h2", {}, "Settings"), h("p", {}, "Everything runs on this computer except the voice model.")),
          h("button", { class: "icon-btn", "aria-label": "Close settings", onclick: () => close() }, icon("x"))),
        body,
        h("div", { class: "sheet-foot" }, h("span", { class: "sheet-note" }, "Voice changes apply the next time you start talking."),
          h("div", { class: "spacer" }), h("button", { class: "btn", onclick: () => close() }, "Cancel"), saveBtn),
      ];
    });
  } finally {
    stopPreview();
    preview.onChange = null;
    settingsOpen = false;
  }
}

function section(title, hint, content) {
  return h("section", { class: "sheet-section" },
    h("div", { class: "sheet-section-head" }, h("h3", {}, title), hint ? h("p", {}, hint) : null), content);
}

function statusRow(ic, label, value, state) {
  return h("div", { class: `status-row ${state}` }, h("span", { class: "status-ic" }, icon(ic, "sm")),
    h("span", { class: "status-label" }, label), h("span", { class: "status-value", title: value }, value),
    h("span", { class: "status-dot" }));
}

function shortcut(keys, label) {
  return h("div", { class: "shortcut" }, h("span", {}, label), h("span", { class: "keys" }, keys.map((k) => h("kbd", {}, k))));
}

function themeSwitch() {
  const current = getTheme();
  const seg = h("div", { class: "seg", role: "radiogroup", "aria-label": "Theme" });
  for (const [value, ic, label] of [["system", "monitor", "System"], ["light", "sun", "Light"], ["dark", "moon", "Dark"]]) {
    seg.append(h("button", {
      class: value === current ? "on" : "", role: "radio", "aria-checked": String(value === current),
      onclick: (e) => {
        setTheme(value);
        seg.querySelectorAll("button").forEach((b) => { b.classList.toggle("on", b === e.currentTarget); b.setAttribute("aria-checked", String(b === e.currentTarget)); });
      },
    }, icon(ic, "sm"), label));
  }
  return seg;
}

async function route() {
  if (cleanup) { try { cleanup(); } catch (e) { console.error(e); } cleanup = null; }
  const hash = location.hash || "#/";
  const match = hash.match(/^#\/nb\/([a-z0-9]+)/i);
  // Dialogs and menus live on <body>, outside the routed view: close them on navigation.
  document.querySelectorAll(".overlay, .menu").forEach((el) => el.remove());
  app.replaceChildren(h("div", { class: "route-loading" }, h("div", { class: "spinner" })));
  try {
    cleanup = match ? await renderNotebook(app, match[1]) : await renderHome(app);
  } catch (e) {
    console.error(e);
    app.replaceChildren(h("div", { class: "empty-hero" },
      h("div", { class: "modal-icon danger" }, icon("alert")),
      h("h2", {}, "Something went wrong"), h("p", {}, e.message),
      h("button", { class: "btn primary", onclick: () => { location.hash = "#/"; } }, "Back to notebooks")));
  }
}

window.addEventListener("hashchange", route);
refreshStatus().then(route);
// Keep the status pill honest while the embedding model warms up.
const statusTimer = setInterval(async () => {
  const s = await refreshStatus();
  if (s && s.embedder !== "loading") clearInterval(statusTimer);
}, 2500);
