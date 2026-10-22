// Server status (embedding model, Gemini key, voice, ...), shared by the top bar pill, Settings and the
// notebook ("can we talk yet?").
import { api } from "./api";
import { createStore } from "./store";
import type { Status } from "./types";

// checked=false until the first answer (or failure), so nothing claims "offline" before we know.
export const statusStore = createStore({ status: null as Status | null, checked: false });

export async function refreshStatus(): Promise<Status | null> {
  let status: Status | null = null;
  try { status = await api.status(); } catch { status = null; }
  statusStore.set({ status, checked: true });
  return status;
}

let polling: ReturnType<typeof setInterval> | null = null;

// Refresh now, then keep the pill honest while the embedding model warms up (stops once it's ready).
export function startStatusPolling() {
  if (polling) return;
  refreshStatus();
  polling = setInterval(async () => {
    const s = await refreshStatus();
    if (s && s.embedder !== "loading" && polling) { clearInterval(polling); polling = null; }
  }, 2500);
}

let watching = false;
let lastWake = 0;

// Polling stops once the model is ready, so a fixed key or a closed API window would otherwise only show up
// after a reload: check again whenever the tab comes back into view (at most every 5 s).
export function startStatusWatch() {
  if (watching) return;
  watching = true;
  const wake = () => {
    if (document.visibilityState !== "visible" || Date.now() - lastWake < 5000) return;
    lastWake = Date.now();
    refreshStatus();
  };
  window.addEventListener("focus", wake);
  document.addEventListener("visibilitychange", wake);
}
