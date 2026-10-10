// Small DOM toolkit: element builder, icons, toasts, dialogs/sheets, menus, progress rings, formatting.
import { LUCIDE } from "./icons.js";

export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "style" && typeof value === "object") {
      for (const [prop, v] of Object.entries(value)) {
        if (prop.startsWith("--")) el.style.setProperty(prop, v);
        else el.style[prop] = v;
      }
    } else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "html") el.innerHTML = value;
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, value);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

// Icons: Lucide (https://lucide.dev, ISC). Each icon gets an `ic-<name>` class so CSS can give it a small,
// meaningful motion on hover/press (see "animated icons" in app.css).
export function icon(name, cls = "") {
  const inner = LUCIDE[name] || LUCIDE.sparkles;
  const span = document.createElement("span");
  span.innerHTML = `<svg class="icon ic-${name} ${cls}" viewBox="0 0 24 24" aria-hidden="true">${inner}</svg>`;
  return span.firstChild;
}

// Notebook icons (stored by the server without the "nb-" prefix).
export const NOTEBOOK_ICONS = ["book", "cap", "flask", "brain", "atom", "globe", "landmark", "code", "calculator",
  "scale", "stethoscope", "palette", "music", "leaf", "rocket", "briefcase", "chart", "languages", "dna",
  "telescope", "pen", "map", "lightbulb", "cpu"];

export function notebookIcon(name, cls = "") {
  return icon(`nb-${NOTEBOOK_ICONS.includes(name) ? name : "book"}`, cls);
}

const TOAST_ICON = { success: "checkCircle", error: "xCircle", info: "info", "": "info" };

export function toast(message, kind = "", ms = 4200) {
  const el = h("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : "status" },
    icon(TOAST_ICON[kind] || "info"), h("span", {}, message));
  document.getElementById("toasts").append(el);
  setTimeout(() => el.classList.add("leaving"), ms);
  setTimeout(() => el.remove(), ms + 400);
}

function openLayer(kind, build) {
  // build(close) returns the content element(s). Resolves with whatever close() is called with.
  return new Promise((resolve) => {
    const prev = document.activeElement;
    const overlay = h("div", { class: `overlay ${kind}-overlay`, role: "dialog", "aria-modal": "true" });
    let closed = false;
    const close = (value) => {
      if (closed) return;
      closed = true;
      overlay.classList.add("leaving");
      document.removeEventListener("keydown", onKey, true);
      setTimeout(() => overlay.remove(), 180);
      if (prev && prev.isConnected) prev.focus?.();
      resolve(value);
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.stopPropagation(); close(undefined); }
    };
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) close(undefined); });
    document.addEventListener("keydown", onKey, true);
    const box = h("div", { class: kind }, build(close));
    overlay.append(box);
    document.body.append(overlay);
    requestAnimationFrame(() => {
      (box.querySelector("[autofocus]") || box.querySelector("input, button.primary, button.accent, button.danger"))?.focus();
    });
  });
}

export const modal = (build) => openLayer("modal", build);
export const sheet = (build) => openLayer("sheet", build);

export function confirmDialog({ title, body, confirm = "Delete", danger = true }) {
  return modal((close) => [
    h("div", { class: `modal-icon ${danger ? "danger" : ""}` }, icon(danger ? "trash" : "info")),
    h("h2", {}, title),
    h("p", {}, body),
    h("div", { class: "actions" },
      h("button", { class: "btn", onclick: () => close(false) }, "Cancel"),
      h("button", { class: `btn ${danger ? "danger" : "primary"}`, onclick: () => close(true) }, confirm)),
  ]);
}

export function promptDialog({ title, body = "", value = "", placeholder = "", confirm = "Save" }) {
  return modal((close) => {
    const input = h("input", { class: "field", value, placeholder, autofocus: true, maxlength: 120 });
    const submit = () => close(input.value.trim() || undefined);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
    return [
      h("h2", {}, title),
      body ? h("p", {}, body) : null,
      input,
      h("div", { class: "actions" },
        h("button", { class: "btn", onclick: () => close(undefined) }, "Cancel"),
        h("button", { class: "btn primary", onclick: submit }, confirm)),
    ];
  });
}

export function openMenu(anchor, items) {
  document.querySelectorAll(".menu").forEach((m) => m.remove());
  const menu = h("div", { class: "menu", role: "menu" },
    items.map((item) => h("button", {
      class: item.danger ? "danger" : "", role: "menuitem",
      onclick: (e) => { e.stopPropagation(); menu.remove(); item.action(); },
    }, item.icon ? icon(item.icon, "sm") : null, item.label)));
  document.body.append(menu);
  const r = anchor.getBoundingClientRect();
  const w = menu.offsetWidth;
  menu.style.top = `${Math.min(r.bottom + 6, window.innerHeight - menu.offsetHeight - 8)}px`;
  menu.style.left = `${Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8))}px`;
  setTimeout(() => {
    const away = (e) => { if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener("mousedown", away); } };
    document.addEventListener("mousedown", away);
  });
  return menu;
}

// Circular progress ring (0-100). Size in px.
export function ring(percent, size = 28, stroke = 3) {
  const p = Math.max(0, Math.min(100, Math.round(percent || 0)));
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const wrap = document.createElement("span");
  wrap.className = "ring";
  wrap.style.width = wrap.style.height = `${size}px`;
  wrap.title = `${p}% covered`;
  wrap.innerHTML = `<svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" aria-hidden="true">
    <circle class="ring-track" cx="${size / 2}" cy="${size / 2}" r="${r}" stroke-width="${stroke}" fill="none"/>
    <circle class="ring-fill" cx="${size / 2}" cy="${size / 2}" r="${r}" stroke-width="${stroke}" fill="none"
      stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - p / 100)}" stroke-linecap="round"
      transform="rotate(-90 ${size / 2} ${size / 2})"/></svg>`;
  return wrap;
}

export function timeAgo(iso) {
  if (!iso) return "";
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "just now";
  const units = [[60, "minute"], [3600, "hour"], [86400, "day"], [604800, "week"], [2629800, "month"], [31557600, "year"]];
  for (let i = units.length - 1; i >= 0; i--) {
    const [size, name] = units[i];
    if (seconds >= size) {
      const n = Math.floor(seconds / size);
      return `${n} ${name}${n > 1 ? "s" : ""} ago`;
    }
  }
  return "";
}

export function clockTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay
    ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export function greeting() {
  const hour = new Date().getHours();
  return hour < 5 ? "Working late" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied", "success", 1600);
  } catch {
    toast("Couldn't copy to the clipboard", "error");
  }
}

export function getTheme() {
  return document.documentElement.dataset.theme || "system";
}

export function isDark() {
  const t = getTheme();
  return t === "dark" || (t === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
}

export function setTheme(theme) {
  if (theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  try { theme === "system" ? localStorage.removeItem("vt-theme") : localStorage.setItem("vt-theme", theme); } catch (e) { /* storage blocked */ }
  window.dispatchEvent(new CustomEvent("vt-theme"));
}
