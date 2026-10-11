// Browser client for the REST API. Requests go to this origin's /api, which Next.js proxies to the
// Python server. Throws Error(detail) on non-2xx; every request has a timeout so a stuck server can
// never freeze the UI.
import type { Job, Located, Notebook, NotebookSummary, Outline, SearchRef, Status } from "./types";

const OFFLINE = "Can't reach the Voice Tutor server. Is it running? Start it with run.bat.";
const SLOW = "The server took too long to answer. Check the “Voice Tutor - API” window or logs\\errors.log.";

async function request<T>(method: string, url: string, body?: unknown, isForm = false, timeoutMs = 20000): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const init: RequestInit = { method, headers: {}, signal: controller.signal };
  if (body !== undefined) {
    if (isForm) init.body = body as FormData;
    else {
      (init.headers as Record<string, string>)["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
  }
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch (e) {
    throw new Error((e as Error).name === "AbortError" ? SLOW : OFFLINE);
  } finally {
    clearTimeout(timer);
  }
  if (res.status === 204) return null as T;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    // A proxy error page (API window closed) is not JSON: say so plainly.
    if (res.status >= 500 && typeof data?.detail !== "string") throw new Error(OFFLINE);
    throw new Error(typeof data?.detail === "string" ? data.detail : `Request failed (${res.status})`);
  }
  return data as T;
}

export const api = {
  status: () => request<Status>("GET", "/api/status", undefined, false, 8000),
  saveSettings: (s: { voice: string }) => request<Status>("PUT", "/api/settings", s),
  notebooks: () => request<NotebookSummary[]>("GET", "/api/notebooks"),
  createNotebook: (name: string) => request<NotebookSummary>("POST", "/api/notebooks", { name }),
  notebook: (id: string) => request<Notebook>("GET", `/api/notebooks/${id}`),
  updateNotebook: (id: string, patch: { name?: string; icon?: string; active_source?: string }) =>
    request<NotebookSummary>("PATCH", `/api/notebooks/${id}`, patch),
  deleteNotebook: (id: string) => request<null>("DELETE", `/api/notebooks/${id}`),
  clearHistory: (id: string) => request<null>("DELETE", `/api/notebooks/${id}/history`),
  search: (id: string, q: string) => request<{ refs: SearchRef[] }>("GET", `/api/notebooks/${id}/search?q=${encodeURIComponent(q)}`),
  upload: (id: string, files: File[]) => {
    const form = new FormData();
    for (const f of files) form.append("files", f, f.name);
    return request<{ jobs: Job[] }>("POST", `/api/notebooks/${id}/sources`, form, true, 10 * 60 * 1000);
  },
  removeSource: (id: string, docId: string) => request<null>("DELETE", `/api/notebooks/${id}/sources/${docId}`),
  job: (jobId: string) => request<Job>("GET", `/api/jobs/${jobId}`),
  dismissJob: (jobId: string) => request<null>("DELETE", `/api/jobs/${jobId}`),
  outline: (docId: string) => request<Outline>("GET", `/api/docs/${docId}/outline`),
  resetProgress: (docId: string) => request<Outline>("POST", `/api/docs/${docId}/reset`),
  // target: a section id, number or title, or "all"; through: everything up to and including it
  markProgress: (docId: string, target: string, completed: boolean, through = false) =>
    request<Outline>("POST", `/api/docs/${docId}/progress`, { target, completed, through }),
  locate: (docId: string, text: string, first: number, last?: number) =>
    request<Located>("GET", `/api/docs/${docId}/locate?first=${first}&last=${last || first}&text=${encodeURIComponent(text.slice(0, 1500))}`),
  pageSizes: (docId: string) => request<{ sizes: [number, number][] }>("GET", `/api/docs/${docId}/pages`),
  pageUrl: (docId: string, page: number, scale: number) => `/api/docs/${docId}/page/${page}.png?scale=${scale}`,
  fileUrl: (docId: string) => `/api/docs/${docId}/file`,
};
