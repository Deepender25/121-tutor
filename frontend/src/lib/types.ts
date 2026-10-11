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
  covered?: number;
  position?: Position;
  missing?: boolean;
  error?: string;
}

export interface SearchRef {
  doc_id: string;
  title: string;
  page: number;
  pages: string;
  section: string;
  snippet: string;
  text?: string; // full chunk text: live events only (follow-along), never in saved history
}

export interface WebLink {
  title: string;
  url: string;
  site: string;
}

export type ToolUI =
  | { kind: "search"; query: string; refs: SearchRef[] }
  | { kind: "passage"; doc_id: string; page: number; pages: string; section: string; position: string; covered: string; snippet: string; text?: string }
  | { kind: "web"; query: string; links: WebLink[] }
  | { kind: "progress"; doc_id: string; completed: boolean; summary: string; covered: string }
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
  status: "queued" | "running" | "done" | "error";
  stage: "queued" | "parsing" | "chunking" | "embedding" | "saving" | "done";
  done: number;
  total: number;
  percent: number;
  error: string | null;
  doc_id: string | null;
  title: string | null;
}

export interface NotebookSummary {
  id: string;
  name: string;
  emoji: string;
  icon: string;
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

export type SectionStatus = "completed" | "in_progress" | "not_started";

export interface OutlineSection {
  id: string;
  title: string;
  level: number;
  pages: string;
  status: SectionStatus;
  covered: boolean; // status === "completed"
}

export interface Outline {
  title: string;
  pages: number;
  sections: OutlineSection[];
  position: Position;
  document_covered: string;
}

export interface Status {
  embedder: "ready" | "loading" | "error";
  embedder_error: string | null;
  gemini_key: boolean;
  live_model: string;
  web_provider: "tavily" | "duckduckgo" | string;
  voices: string[];
  log_file: string;
  voice: string;
}

export interface Located {
  page: number | null;
  rects: number[][];
}
