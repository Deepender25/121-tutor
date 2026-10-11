"""Notebooks: a named collection of sources (indexed PDFs) plus its conversation history.

Stored as one JSON file per notebook in DATA_ROOT/notebooks/. A source's index lives in docs/<doc_id>/
and can be shared by several notebooks; it's deleted only when no notebook uses it any more.
"""

import json
import random
import secrets
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

HISTORY_CAP = 300
# Notebook icons (Lucide names, without the "nb-" prefix the frontend uses).
ICONS = ["book", "cap", "flask", "brain", "atom", "globe", "landmark", "code", "calculator", "scale",
         "stethoscope", "palette", "music", "leaf", "rocket", "briefcase", "chart", "languages", "dna",
         "telescope", "pen", "map", "lightbulb", "cpu"]
DEFAULT_ICON = "book"
# Book-cover colours (the frontend's lib/covers.ts has the same keys, in the same order). Each notebook keeps one;
# a new notebook gets the least-used colour, so the books on the shelf stay distinct.
COVERS = ["oxblood", "navy", "forest", "ochre", "slate", "terracotta", "plum", "teal", "sand", "olive", "rose",
          "graphite"]
# Notebooks created before icons existed stored an emoji; map it to the closest icon.
_EMOJI_TO_ICON = {
    "📘": "book", "📗": "book", "📙": "book", "📕": "book", "📓": "book", "📜": "book", "🧪": "flask", "🔬": "flask",
    "🧠": "brain", "🌍": "globe", "🌊": "globe", "⚙️": "cpu", "💻": "code", "🎓": "cap", "🔭": "telescope",
    "🧬": "dna", "📐": "pen", "✍️": "pen", "🏛️": "landmark", "🏗️": "landmark", "🕰️": "landmark", "💡": "lightbulb",
    "🌱": "leaf", "🎵": "music", "💼": "briefcase", "⚖️": "scale", "🩺": "stethoscope", "📊": "chart", "🗺️": "map",
    "🧮": "calculator", "🎨": "palette", "🚀": "rocket",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Notebook:
    id: str
    name: str
    emoji: str  # legacy (pre-icon notebooks); kept so old files load unchanged
    created_at: str
    updated_at: str
    sources: list[str] = field(default_factory=list)  # doc_ids, in the order added
    active_source: str | None = None  # the source being taught (reading cursor)
    history: list[dict] = field(default_factory=list)  # {role, text, ts, refs?}
    icon: str = ""  # legacy: notebooks are shown as books now (cover below)
    cover: str = ""  # one of COVERS

    def __post_init__(self):
        if self.icon not in ICONS:
            self.icon = _EMOJI_TO_ICON.get(self.emoji, DEFAULT_ICON)

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("history")
        d["turns"] = len(self.history)
        return d


class NotebookStore:
    def __init__(self, folder: Path):
        self.folder = folder
        self._lock = threading.RLock()

    def _path(self, notebook_id: str) -> Path:
        if not notebook_id.isalnum():
            raise KeyError(notebook_id)
        return self.folder / f"{notebook_id}.json"

    def _write(self, nb: Notebook) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self._path(nb.id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(nb), indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def _load(self, path: Path) -> Notebook:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Notebook(**{k: v for k, v in data.items() if k in Notebook.__dataclass_fields__})

    def _least_used_cover(self, notebooks: list[Notebook]) -> str:
        used = [nb.cover for nb in notebooks if nb.cover in COVERS]
        return min(COVERS, key=lambda c: (used.count(c), COVERS.index(c)))

    def _assign_covers(self, notebooks: list[Notebook]) -> None:
        """Notebooks from before covers existed get one, oldest first, and keep it from then on."""
        for nb in sorted(notebooks, key=lambda n: n.created_at):
            if nb.cover not in COVERS:
                nb.cover = self._least_used_cover(notebooks)
                self._write(nb)

    def all(self) -> list[Notebook]:
        with self._lock:
            if not self.folder.exists():
                return []
            notebooks = [self._load(p) for p in self.folder.glob("*.json")]
            self._assign_covers(notebooks)
            return sorted(notebooks, key=lambda n: n.updated_at, reverse=True)

    def get(self, notebook_id: str) -> Notebook:
        with self._lock:
            path = self._path(notebook_id)
            if not path.exists():
                raise KeyError(notebook_id)
            nb = self._load(path)
            if nb.cover not in COVERS:
                self.all()  # assigns every missing cover consistently
                nb = self._load(path)
            return nb

    def create(self, name: str, icon: str | None = None) -> Notebook:
        with self._lock:
            now = _now()
            nb = Notebook(secrets.token_hex(6), name.strip() or "Untitled notebook", "", now, now,
                          icon=icon if icon in ICONS else random.choice(ICONS), cover=self._least_used_cover(self.all()))
            self._write(nb)
            return nb

    def update(self, notebook_id: str, **changes) -> Notebook:
        with self._lock:
            nb = self.get(notebook_id)
            for key in ("name", "active_source"):
                if key in changes and changes[key] is not None:
                    setattr(nb, key, changes[key])
            if changes.get("icon") in ICONS:
                nb.icon = changes["icon"]
            if changes.get("cover") in COVERS:
                nb.cover = changes["cover"]
            nb.name = nb.name.strip()[:120] or "Untitled notebook"
            nb.updated_at = _now()
            self._write(nb)
            return nb

    def delete(self, notebook_id: str) -> list[str]:
        """Delete a notebook; returns its doc_ids no other notebook uses (safe to remove)."""
        with self._lock:
            nb = self.get(notebook_id)
            self._path(notebook_id).unlink()
            return [d for d in nb.sources if not self.is_used(d)]

    def add_source(self, notebook_id: str, doc_id: str) -> Notebook:
        with self._lock:
            nb = self.get(notebook_id)
            if doc_id not in nb.sources:
                nb.sources.append(doc_id)
            if nb.active_source is None:
                nb.active_source = doc_id
            nb.updated_at = _now()
            self._write(nb)
            return nb

    def remove_source(self, notebook_id: str, doc_id: str) -> bool:
        """Remove a source from a notebook. Returns True if no notebook uses it any more."""
        with self._lock:
            nb = self.get(notebook_id)
            if doc_id in nb.sources:
                nb.sources.remove(doc_id)
            if nb.active_source == doc_id:
                nb.active_source = nb.sources[0] if nb.sources else None
            nb.updated_at = _now()
            self._write(nb)
            return not self.is_used(doc_id)

    def is_used(self, doc_id: str) -> bool:
        with self._lock:
            return any(doc_id in nb.sources for nb in self.all())

    def append_history(self, notebook_id: str, entries: list[dict]) -> None:
        with self._lock:
            nb = self.get(notebook_id)
            stamp = _now()
            nb.history.extend({**e, "ts": e.get("ts", stamp)} for e in entries if e.get("text", "").strip())
            nb.history = nb.history[-HISTORY_CAP:]
            nb.updated_at = stamp
            self._write(nb)

    def clear_history(self, notebook_id: str) -> None:
        with self._lock:
            nb = self.get(notebook_id)
            nb.history = []
            nb.updated_at = _now()
            self._write(nb)
