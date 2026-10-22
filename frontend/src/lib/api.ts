// Browser client for the REST API. Locally, requests go to this origin's /api, which Next.js proxies to the
// Python server. Hosted (NEXT_PUBLIC_API_URL set at build time, e.g. https://api.tutor.edgedrop.app), the
// browser calls the API directly with the session cookie. Throws Error(detail) on non-2xx; a 401 sends you to
// the sign-in page. Every request has a timeout so a stuck server can never freeze the UI.
import type { Account, Job, Located, Notebook, NotebookSummary, Outline, SearchRef, Status } from "./types";

export const API_BASE = (process.env.NEXT_PUBLIC_API_URL || "").replace(/\/+$/, "");

/** Absolute API URL for links, images and audio (same-origin /api locally). */
export const apiUrl = (path: string) => `${API_BASE}${path}`;

/** WebSocket URL for an API path: the API's host when hosted, this page's host locally. */
export function apiWsUrl(path: string): string {
  if (API_BASE) return `${API_BASE.replace(/^http/, "ws")}${path}`;
  return `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${path}`;
}

/** Where to go after signing in: only paths on this site (never another site: no open redirects). */
export function safeNext(next: string | null | undefined): string {
  return next && next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/\\") && !next.startsWith("/login") ? next : "/";
}

function toSignIn() {
  if (typeof window === "undefined" || location.pathname.startsWith("/login")) return;
  location.assign(`/login?next=${encodeURIComponent(location.pathname + location.search)}`);
}

/** Thrown for a non-2xx answer: the server's message, plus the HTTP status. */
export type HttpError = Error & { status?: number };

const OFFLINE = "Can't reach the Voice Tutor server. Is it running? Start it with run.bat.";
const SLOW = "The server took too long to answer. Check the “Voice Tutor - API” window or logs\\errors.log.";

async function request<T>(method: string, url: string, body?: unknown, isForm = false, timeoutMs = 20000): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  // credentials: the session cookie also goes to the API's own host when hosted (same site, other origin).
  const init: RequestInit = { method, headers: {}, signal: controller.signal, credentials: "include" };
  if (body !== undefined) {
    if (isForm) init.body = body as FormData;
    else {
      (init.headers as Record<string, string>)["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
  }
  let res: Response;
  try {
    res = await fetch(apiUrl(url), init);
  } catch (e) {
    throw new Error((e as Error).name === "AbortError" ? SLOW : OFFLINE);
  } finally {
    clearTimeout(timer);
  }
  if (res.status === 204) return null as T;
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !url.startsWith("/api/auth/")) toSignIn();
  if (!res.ok) {
    // A proxy error page (API window closed) is not JSON: say so plainly.
    const message = res.status >= 500 && typeof data?.detail !== "string" ? OFFLINE
      : typeof data?.detail === "string" ? data.detail : `Request failed (${res.status})`;
    const err = new Error(message) as HttpError;
    err.status = res.status; // callers can tell "gone" (404) from "try again" (5xx)
    throw err;
  }
  return data as T;
}

// Uploads go through XMLHttpRequest: fetch can't report upload progress, and a 15 MB PDF can take most of a
// minute to send. Same errors and sign-in handling as request().
function uploadForm<T>(url: string, form: FormData, onProgress?: (sent: number, total: number) => void): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", apiUrl(url));
    xhr.withCredentials = true;
    xhr.timeout = 15 * 60 * 1000;
    xhr.responseType = "json";
    if (onProgress) xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded, e.total); };
    xhr.onload = () => {
      const data = (xhr.response || {}) as { detail?: unknown };
      if (xhr.status === 401) toSignIn();
      if (xhr.status >= 200 && xhr.status < 300) resolve(data as T);
      else if (xhr.status >= 500 && typeof data.detail !== "string") reject(new Error(OFFLINE));
      else reject(new Error(typeof data.detail === "string" ? data.detail : `Upload failed (${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error(OFFLINE));
    xhr.ontimeout = () => reject(new Error(SLOW));
    xhr.send(form);
  });
}

export const api = {
  status: () => request<Status>("GET", "/api/status", undefined, false, 8000),
  saveSettings: (s: { voice?: string; style?: Record<string, string> }) => request<Status>("PUT", "/api/settings", s),
  notebooks: () => request<NotebookSummary[]>("GET", "/api/notebooks"),
  createNotebook: (name: string) => request<NotebookSummary>("POST", "/api/notebooks", { name }),
  notebook: (id: string) => request<Notebook>("GET", `/api/notebooks/${id}`),
  updateNotebook: (id: string, patch: { name?: string; icon?: string; cover?: string; active_source?: string }) =>
    request<NotebookSummary>("PATCH", `/api/notebooks/${id}`, patch),
  deleteNotebook: (id: string) => request<null>("DELETE", `/api/notebooks/${id}`),
  clearHistory: (id: string) => request<null>("DELETE", `/api/notebooks/${id}/history`),
  search: (id: string, q: string) => request<{ refs: SearchRef[] }>("GET", `/api/notebooks/${id}/search?q=${encodeURIComponent(q)}`),
  upload: (id: string, files: File[], onProgress?: (sent: number, total: number) => void) => {
    const form = new FormData();
    for (const f of files) form.append("files", f, f.name);
    return uploadForm<{ jobs: Job[] }>(`/api/notebooks/${id}/sources`, form, onProgress);
  },
  // A PDF file on the web, or a web page (read with TinyFish on the server): queued like an upload.
  addLink: (id: string, url: string) => request<{ jobs: Job[] }>("POST", `/api/notebooks/${id}/sources/link`, { url }),
  removeSource: (id: string, docId: string) => request<null>("DELETE", `/api/notebooks/${id}/sources/${docId}`),
  job: (jobId: string) => request<Job>("GET", `/api/jobs/${jobId}`),
  dismissJob: (jobId: string) => request<null>("DELETE", `/api/jobs/${jobId}`),
  outline: (docId: string) => request<Outline>("GET", `/api/docs/${docId}/outline`),
  locate: (docId: string, text: string, first: number, last?: number) =>
    request<Located>("GET", `/api/docs/${docId}/locate?first=${first}&last=${last || first}&text=${encodeURIComponent(text.slice(0, 1500))}`),
  pageSizes: (docId: string) => request<{ sizes: [number, number][] }>("GET", `/api/docs/${docId}/pages`),
  pageUrl: (docId: string, page: number, scale: number) => apiUrl(`/api/docs/${docId}/page/${page}.png?scale=${scale}`),
  fileUrl: (docId: string) => apiUrl(`/api/docs/${docId}/file`),
  voicePreviewUrl: (voice: string) => apiUrl(`/api/voices/${voice}/preview`),
  // sign-in (VOICE_TUTOR_AUTH on the server; {auth: "off"} locally)
  me: () => request<Account>("GET", "/api/auth/me", undefined, false, 8000),
  login: (email: string, password: string) => request<Account>("POST", "/api/auth/login", { email, password }),
  logout: () => request<{ ok: true }>("POST", "/api/auth/logout"),
  logoutAll: () => request<{ ok: true }>("POST", "/api/auth/logout-all"),
};
