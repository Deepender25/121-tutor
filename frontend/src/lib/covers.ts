// Book covers. A notebook keeps the cloth colour it was given (the least-used one when it was made; changeable
// from its menu), so the books on the shelf stay distinct. Each PDF also has a colour of its own, from its content
// hash, used for its spine in the sources list: the same file always looks the same.
import type { NotebookSummary } from "./types";

export interface Cover {
  key: string; // as stored on the notebook (tutor/notebooks.py COVERS)
  name: string;
  bg: string; // the cloth
  ink: string; // title colour that reads on it
}

// Book-cloth tones: muted, print-like, distinct from one another in both themes.
export const COVERS: Cover[] = [
  { key: "oxblood", name: "Oxblood", bg: "#7b2d2d", ink: "#f6e9e4" },
  { key: "navy", name: "Navy", bg: "#1f355a", ink: "#e8edf6" },
  { key: "forest", name: "Forest", bg: "#2e5b46", ink: "#e6f0ea" },
  { key: "ochre", name: "Ochre", bg: "#c7922e", ink: "#2b1e06" },
  { key: "slate", name: "Slate", bg: "#46525f", ink: "#edf0f3" },
  { key: "terracotta", name: "Terracotta", bg: "#b3573d", ink: "#fbeee8" },
  { key: "plum", name: "Plum", bg: "#5b3b67", ink: "#f1e8f4" },
  { key: "teal", name: "Teal", bg: "#2a6a67", ink: "#e4f2f1" },
  { key: "sand", name: "Sand", bg: "#d9c8a4", ink: "#2d2617" },
  { key: "olive", name: "Olive", bg: "#626838", ink: "#f1f2e4" },
  { key: "rose", name: "Rose", bg: "#a3566a", ink: "#fbecef" },
  { key: "graphite", name: "Graphite", bg: "#2c2c31", ink: "#ececf0" },
];

/** The cover of one PDF (stable: FNV-1a hash of its doc id). */
export function coverFor(docId: string): Cover {
  let h = 0x811c9dc5;
  for (let i = 0; i < docId.length; i++) {
    h ^= docId.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return COVERS[(h >>> 0) % COVERS.length];
}

const BY_KEY = new Map(COVERS.map((c) => [c.key, c]));

export function coverByKey(key: string | null | undefined): Cover {
  return BY_KEY.get(key || "") || COVERS[0];
}

/** A notebook's cover: the one it keeps; before the server has assigned one, a stable pick from its id. */
export function notebookCover(nb: Pick<NotebookSummary, "id" | "cover">): Cover {
  return BY_KEY.get(nb.cover) || coverFor(nb.id);
}
