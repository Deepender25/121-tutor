"""The tools the voice model can call, scoped to one notebook's sources.

Provider-neutral: JSON-schema declarations + dict-in/dict-out handlers. Each call returns
(result for the model, event for the UI). Errors come back as {"error": ...} so the model can say
what went wrong instead of the session failing.
"""

import logging
import re
from dataclasses import dataclass
from typing import Callable

from tutor.lesson import LessonError
from tutor.library import DocumentCache, DocumentError
from tutor.retrieval.search import hybrid_search
from tutor.web import web_search

log = logging.getLogger("tools")

SOURCE_PARAM = {
    "type": "string",
    "description": "Which source: its number from the source list (e.g. '2') or words from its title. "
                   "Omit for the active source.",
}

DECLARATIONS = [
    {
        "name": "get_outline",
        "description": (
            "List the notebook's sources and show the outline (sections, pages, what's been covered) of the "
            "active source or a chosen one, plus the current reading position. Use when the user asks what the "
            "material contains, where we are, what's left, or to resolve a navigation request."
        ),
        "parameters": {"type": "object", "properties": {"source": SOURCE_PARAM}},
    },
    {
        "name": "read_passage",
        "description": (
            "Read the next passage of a source in reading order and move the reading position there. Use to "
            "teach step by step: start, continue, repeat, go back, or jump to a section or page. Choosing a "
            "different source makes it the active one. If is_section_end is false and you're teaching the whole "
            "section, call again with target 'next' to get the rest before explaining. Explain every point in "
            "the passage in your own words; don't read it out verbatim unless asked."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "'next' (default), 'current', 'previous', 'start', a section id from get_outline like "
                        "'s012', a page like 'page:40', or a section number/title like 'chapter 3' or '2.1'."
                    ),
                },
                "source": SOURCE_PARAM,
            },
        },
    },
    {
        "name": "search_sources",
        "description": (
            "Search the notebook's sources for passages relevant to a question. Call this BEFORE answering any "
            "question about the material. Write the query in the sources' language with the key terms (names, "
            "numbers, section labels). Use top_k 8 for broad topics (histories, lists, comparisons) so nothing "
            "is missed. Searches all sources unless one is given. Does not move the reading position."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for."},
                "source": SOURCE_PARAM,
                "top_k": {"type": "integer", "description": "Number of passages, 1-8. Default 5."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "update_progress",
        "description": (
            "Mark sections of a source as completed or not completed: this is what shows as ticked in the "
            "person's outline and in the covered percentage. Use it whenever the person asks to mark, tick, "
            "finish, skip or unmark something ('mark everything up to 14 as done', 'I already know chapter 2', "
            "'mark 3.1 as not done', 'mark it all complete'), and after you have fully taught a whole section "
            "without read_passage (read_passage already marks what it reads). Never claim to have marked "
            "anything without calling this."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "The section: its number ('14', '3.2', 'chapter 2'), words from its title, its id from "
                        "get_outline ('s012'), or 'all' for the whole source."
                    ),
                },
                "completed": {
                    "type": "boolean",
                    "description": "true to mark completed (default), false to mark not completed.",
                },
                "through": {
                    "type": "boolean",
                    "description": (
                        "true = everything from the start of the source up to and including target "
                        "('up to 14', 'till section 3'). false (default) = just that section and its subsections."
                    ),
                },
                "source": SOURCE_PARAM,
            },
            "required": ["target"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Search the web. You MUST call this whenever the person asks you to search the internet, the web, "
            "online or Google, or asks what's current or latest, in any language. Also use it to verify a claim, "
            "check whether the sources are outdated, or explain background they assume. Never claim to have "
            "searched without calling this. Say a short filler like 'Let me search that' first."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Web search query."}},
            "required": ["query"],
        },
    },
]


def _pages(start: int, end: int) -> str:
    return str(start) if start == end else f"{start}-{end}"


@dataclass
class ToolOutcome:
    result: dict  # sent back to the model
    ui: dict | None = None  # sent to the browser (citations, page to show, web links)


class NotebookTools:
    def __init__(
        self,
        cache: DocumentCache,
        embedder,
        doc_ids: list[str],
        active: str | None,
        on_active_change: Callable[[str], None] | None = None,
    ):
        self.cache = cache
        self.embedder = embedder
        self.doc_ids = list(doc_ids)
        self.active = active if active in self.doc_ids else (self.doc_ids[0] if self.doc_ids else None)
        self.on_active_change = on_active_change

    # ---- source resolution -----------------------------------------------------------

    def _title(self, doc_id: str) -> str:
        return self.cache.get(doc_id).meta["title"]

    def _number(self, doc_id: str) -> int:
        return self.doc_ids.index(doc_id) + 1

    def resolve_source(self, ref: str | None) -> str:
        if not self.doc_ids:
            raise LessonError("This notebook has no sources yet. Ask the user to add a PDF.")
        if ref is None or not str(ref).strip():
            return self.active
        ref = str(ref).strip().lower()
        if m := re.fullmatch(r"(?:source\s*)?#?(\d+)", ref):
            n = int(m.group(1))
            if 1 <= n <= len(self.doc_ids):
                return self.doc_ids[n - 1]
        if ref in self.doc_ids:
            return ref
        matches = [d for d in self.doc_ids if ref in self._title(d).lower()]
        if len(matches) == 1:
            return matches[0]
        listing = "; ".join(f"{self._number(d)}: {self._title(d)}" for d in self.doc_ids)
        raise LessonError(f"Couldn't tell which source '{ref}' means. Sources are {listing}.")

    def _set_active(self, doc_id: str) -> None:
        if doc_id != self.active:
            self.active = doc_id
            if self.on_active_change:
                self.on_active_change(doc_id)

    # ---- tools -----------------------------------------------------------------------

    def get_outline(self, source: str | None = None) -> ToolOutcome:
        doc_id = self.resolve_source(source)
        sources = []
        for d in self.doc_ids:
            lesson = self.cache.get(d).lesson
            sources.append({
                "number": self._number(d),
                "title": lesson.title,
                "pages": lesson.outline(max_level=1)["pages"],
                "covered": f"{lesson.percent_covered()}%",
                "active": d == self.active,
            })
        outline = self.cache.get(doc_id).lesson.outline(max_level=2)
        return ToolOutcome({"sources": sources, "outline_of_source": self._number(doc_id), "outline": outline})

    def read_passage(self, target: str = "next", source: str | None = None) -> ToolOutcome:
        doc_id = self.resolve_source(source)
        passage = self.cache.get(doc_id).lesson.read(target or "next")
        self._set_active(doc_id)
        result = {"source": self._title(doc_id), "source_number": self._number(doc_id), **passage}
        first_page = int(passage["pages"].split("-")[0])
        ui = {"kind": "passage", "doc_id": doc_id, "page": first_page, "pages": passage["pages"],
              "section": passage["section"], "position": passage["position"],
              "covered": passage["document_covered"], "snippet": passage["text"][:240], "text": passage["text"]}
        return ToolOutcome(result, ui)

    def search_sources(self, query: str, source: str | None = None, top_k: int = 5) -> ToolOutcome:
        top_k = max(1, min(int(top_k or 5), 8))
        doc_ids = [self.resolve_source(source)] if source else self.doc_ids
        if not doc_ids:
            raise LessonError("This notebook has no sources yet.")
        docs = {d: self.cache.get(d) for d in doc_ids}
        # Vectors only from tables in the current embedder's space; documents still being re-embedded after a mode
        # switch, or a question that can't be embedded right now, fall back to keyword matching.
        vector_stores = {d: doc.store for d, doc in docs.items() if doc.store is not None}
        notes = []
        query_vector = None
        if vector_stores:
            try:
                query_vector = self.embedder.embed_query(query)
            except Exception as e:
                log.warning("Couldn't embed the search query (%s); using keyword search only", e)
                notes.append(f"Meaning-based search is unavailable right now ({e}); these are keyword matches only.")
        pending = [self._title(d) for d, doc in docs.items() if doc.store is None]
        if pending:
            notes.append(f"Still being indexed for the current embedding model, so keyword matches only: {'; '.join(pending)}.")
        hits = hybrid_search({d: doc.text_store for d, doc in docs.items()}, query, query_vector, top_k,
                             vector_stores=vector_stores)
        note = {"note": " ".join(notes)} if notes else {}
        if not hits:
            return ToolOutcome({"results": [], "note": " ".join(["Nothing in the sources matched.", *notes])},
                               {"kind": "search", "query": query, "refs": []})
        results, refs = [], []
        for h in hits:
            pages = _pages(h["page_start"], h["page_end"])
            title = self._title(h["doc_id"])
            results.append({"source": title, "source_number": self._number(h["doc_id"]),
                            "section": h["heading_path"], "pages": pages, "text": h["text"]})
            refs.append({"doc_id": h["doc_id"], "title": title, "page": h["page_start"], "pages": pages,
                         "section": h["heading_path"], "snippet": " ".join(h["text"].split())[:240],
                         "text": h["text"]})
        return ToolOutcome({"results": results, **note}, {"kind": "search", "query": query, "refs": refs})

    def update_progress(self, target: str, completed: bool = True, through: bool = False,
                        source: str | None = None) -> ToolOutcome:
        doc_id = self.resolve_source(source)
        lesson = self.cache.get(doc_id).lesson
        result = lesson.mark(target, completed=bool(completed), through=bool(through))
        summary = f"{'Marked' if result['completed'] else 'Unmarked'} {result['marked']}"
        ui = {"kind": "progress", "doc_id": doc_id, "completed": result["completed"], "summary": summary,
              "covered": result["document_covered"]}
        return ToolOutcome({"source": self._title(doc_id), **result}, ui)

    def web_search(self, query: str) -> ToolOutcome:
        result = web_search(query)
        links = [{"title": r["title"], "url": r["url"], "site": r["site"]} for r in result.get("results", [])]
        return ToolOutcome(result, {"kind": "web", "query": query, "links": links})

    def call(self, name: str, args: dict | None) -> ToolOutcome:
        handler = {
            "get_outline": self.get_outline,
            "read_passage": self.read_passage,
            "search_sources": self.search_sources,
            "update_progress": self.update_progress,
            "web_search": self.web_search,
        }.get(name)
        if handler is None:
            return ToolOutcome({"error": f"Unknown tool {name}"})
        try:
            return handler(**(args or {}))
        except (LessonError, DocumentError) as e:
            return ToolOutcome({"error": str(e)}, {"kind": "error", "message": str(e)})
        except TypeError as e:
            return ToolOutcome({"error": f"Bad arguments for {name}: {e}"})
