// Server-side data loading for server-rendered pages. Talks to the Python API directly (not through
// the /api rewrite): locally on 127.0.0.1, hosted at VOICE_TUTOR_API_URL (default: NEXT_PUBLIC_API_URL).
// The visitor's cookies are passed along so the API sees their session. Never imported by client components.
import { headers } from "next/headers";
import { connection } from "next/server";
import { cache } from "react";
import type { Notebook, NotebookSummary } from "./types";

const API = (process.env.VOICE_TUTOR_API_URL || process.env.NEXT_PUBLIC_API_URL
  || `http://${process.env.VOICE_TUTOR_HOST || "127.0.0.1"}:${process.env.VOICE_TUTOR_API_PORT || "8766"}`).replace(/\/+$/, "");

export type Loaded<T> = { kind: "ok"; data: T } | { kind: "missing" } | { kind: "offline" } | { kind: "signin" };

async function load<T>(path: string): Promise<Loaded<T>> {
  await connection(); // always render per request: notebooks change all the time
  const cookie = (await headers()).get("cookie");
  let res: Response;
  try {
    res = await fetch(`${API}${path}`, {
      cache: "no-store",
      signal: AbortSignal.timeout(15000),
      headers: cookie ? { cookie } : {},
    });
  } catch {
    return { kind: "offline" };
  }
  if (res.status === 401) return { kind: "signin" };
  if (res.status === 404) return { kind: "missing" };
  if (!res.ok) return { kind: "offline" };
  return { kind: "ok", data: (await res.json()) as T };
}

export const loadNotebooks = () => load<NotebookSummary[]>("/api/notebooks");

// cache(): the page and its metadata share one request to the API per page load.
export const loadNotebook = cache(async (id: string): Promise<Loaded<Notebook>> => {
  // Notebook ids are short hex strings; anything else can't exist (and never reaches the API).
  if (!/^[a-z0-9]{1,64}$/i.test(id)) return { kind: "missing" };
  return load<Notebook>(`/api/notebooks/${id}`);
});
