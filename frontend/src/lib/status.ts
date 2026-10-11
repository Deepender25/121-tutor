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
