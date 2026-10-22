// Shapes of the FastAPI responses (tutor/server/app.py).

export type Position =
  | { started: false }
  | { started: true; section_id: string; section: string; pages: string; part: string };

export interface Source {
  doc_id: string;
  title: string;
  file_name?: string;
  pages?: number;
  chunks?: number;
  position?: Position;
  missing?: boolean;
  error?: string;
  keyword_only?: boolean; // indexed with the other embedding model: keyword search until it's re-embedded
}

export interface SearchRef {
  doc_id: string;
  title: string;
  page: number;
  pages: string;
  section: string;
  snippet: string;
}

export interface WebLink {
  title: string;
  url: string;
  site: string;
}

export type ToolUI =
  | { kind: "search"; query: string; refs: SearchRef[] }
  | { kind: "passage"; doc_id: string; page: number; pages: string; section: string; position: string; snippet: string }
  | { kind: "web"; query: string; links: WebLink[]; via?: string } // via: the service that answered (TinyFish, ...)
  | { kind: "error"; message: string };

export interface HistoryEntry {
  role: "user" | "model";
  text: string;
  ts?: string;
  refs?: ToolUI[];
}

export interface Job {
  id: string;
  notebook_id: string;
  file_name: string;
  status: "uploading" | "queued" | "running" | "done" | "error"; // uploading: still on its way (browser only)
  stage: "uploading" | "queued" | "fetching" | "parsing" | "chunking" | "embedding" | "saving" | "done"; // fetching: a link
  done: number;
  total: number;
  percent: number;
  error: string | null;
  doc_id: string | null;
  title: string | null;
  kind: "ingest" | "reindex"; // reindex: an existing source getting vectors for the current embedding model
  stage_seconds?: number; // how long the current stage has been running (server clock)
}

export interface NotebookSummary {
  id: string;
  name: string;
  emoji: string;
  icon: string;
  cover: string; // book-cover colour key (lib/covers.ts)
  created_at: string;
  updated_at: string;
  sources: Source[];
  active_source: string | null;
  turns: number;
}

export interface Notebook extends NotebookSummary {
  history: HistoryEntry[];
  jobs: Job[];
}

export interface OutlineSection {
  id: string;
  title: string;
  level: number;
  pages: string;
}

export interface Outline {
  title: string;
  pages: number;
  sections: OutlineSection[];
  position: Position;
}

export interface Status {
  embedder: "ready" | "loading" | "error";
  embedder_error: string | null;
  embedding_mode: "offline" | "online"; // EMBEDDING_MODEL in .env
  embedding_model: string;
  auth: "on" | "off"; // VOICE_TUTOR_AUTH: sign-in required (hosted)
  gemini_key: boolean;
  live_model: string;
  web_provider: "tinyfish" | "tavily" | "duckduckgo" | string; // WEB_SEARCH in .env
  web_search?: string; // a description, e.g. "TinyFish (Search, and Fetch reads the top pages)"
  web_error?: string | null; // WEB_SEARCH can't be used as set (unknown value, missing key)
  voices: string[];
  log_file: string;
  voice: string;
  style: TutorStyle; // the tutor's manner (tutor/voice/style.py)
  style_options?: Record<string, { label: string; options: { value: string; label: string; hint: string }[] }>;
}

export type TutorStyle = Record<string, string>; // warmth, expression, teaching

export type Account = { auth: "off" } | { auth: "on"; email: string };

export interface Located {
  page: number | null;
  rects: number[][];
}
