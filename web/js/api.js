// Thin client for the local REST API. Throws Error(detail) on non-2xx.

async function request(method, url, body, isForm = false, timeoutMs = 20000) {
  // Every request has a timeout so a stuck server can never freeze the UI.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const options = { method, headers: {}, signal: controller.signal };
  if (body !== undefined) {
    if (isForm) options.body = body;
    else {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
  }
  let res;
  try {
    res = await fetch(url, options);
  } catch (e) {
    throw new Error(e.name === "AbortError"
      ? "The server took too long to answer. Check the “Voice Tutor - Server” window or logs\\errors.log."
      : "Can't reach the Voice Tutor server. Is it running? Start it with run.bat.");
  } finally {
    clearTimeout(timer);
  }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof data.detail === "string" ? data.detail : `Request failed (${res.status})`;
    throw new Error(detail);
  }
  return data;
}

export const api = {
  status: () => request("GET", "/api/status", undefined, false, 8000),
  saveSettings: (s) => request("PUT", "/api/settings", s),
  notebooks: () => request("GET", "/api/notebooks"),
  createNotebook: (name, emoji) => request("POST", "/api/notebooks", { name, emoji }),
  notebook: (id) => request("GET", `/api/notebooks/${id}`),
  updateNotebook: (id, patch) => request("PATCH", `/api/notebooks/${id}`, patch),
  deleteNotebook: (id) => request("DELETE", `/api/notebooks/${id}`),
  clearHistory: (id) => request("DELETE", `/api/notebooks/${id}/history`),
  search: (id, q) => request("GET", `/api/notebooks/${id}/search?q=${encodeURIComponent(q)}`),
  upload: (id, files) => {
    const form = new FormData();
    for (const f of files) form.append("files", f, f.name);
    return request("POST", `/api/notebooks/${id}/sources`, form, true, 10 * 60 * 1000);
  },
  removeSource: (id, docId) => request("DELETE", `/api/notebooks/${id}/sources/${docId}`),
  job: (jobId) => request("GET", `/api/jobs/${jobId}`),
  dismissJob: (jobId) => request("DELETE", `/api/jobs/${jobId}`),
  outline: (docId) => request("GET", `/api/docs/${docId}/outline`),
  resetProgress: (docId) => request("POST", `/api/docs/${docId}/reset`),
  find: (docId, page, text) => request("GET", `/api/docs/${docId}/page/${page}/find?text=${encodeURIComponent(text.slice(0, 1500))}`),
  locate: (docId, text, first, last) => request("GET", `/api/docs/${docId}/locate?first=${first}&last=${last || first}&text=${encodeURIComponent(text.slice(0, 1500))}`),
  pageSizes: (docId) => request("GET", `/api/docs/${docId}/pages`),
  pageUrl: (docId, page, scale) => `/api/docs/${docId}/page/${page}.png?scale=${scale}`,
  fileUrl: (docId) => `/api/docs/${docId}/file`,
};
