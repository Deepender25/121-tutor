"""Reading position over a document's ordered chunks.

Position lives here, not in the voice model's memory: context compression drops old turns, so the
model is told where we are on every (re)connect and moves through the document only via read().
"""

import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tutor import fileio
from tutor.document import Chunk, Section

log = logging.getLogger("lesson")
_save_lock = threading.Lock()  # tool calls run in worker threads: one progress write at a time


class LessonError(ValueError):
    """A target that can't be resolved. The message is written to be relayed by the model."""


@dataclass
class Progress:
    cursor: int = -1  # order of the chunk last read; -1 = not started
    last_summary: str = ""
    updated_at: str = ""

    @classmethod
    def load(cls, path: Path) -> "Progress":
        """Older files also hold read/done chunk lists (completion tracking, since removed); they're ignored."""
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(int(data.get("cursor", -1)), data.get("last_summary", ""), data.get("updated_at", ""))

    def save(self, path: Path) -> None:
        data = {"cursor": self.cursor, "last_summary": self.last_summary, "updated_at": self.updated_at}
        with _save_lock:
            fileio.write_json(path, data)


def _pages(start: int, end: int) -> str:
    return str(start) if start == end else f"{start}-{end}"


_PAGE = re.compile(r"^page[\s:]*(\d+)$", re.IGNORECASE)
_SECTION_ID = re.compile(r"^s\d+$")
_NUMBER_REF = re.compile(r"^(?:chapter|section|part|unit|lesson|module|point|topic|ch\.?|sec\.?)?\s*(\d+(?:\.\d+)*)\.?$")
_LEADING_NUMBER = re.compile(r"^(?:chapter|section|part|unit|lesson|module)?\s*(\d+(?:\.\d+)*)\b")


class Lesson:
    def __init__(self, title: str, sections: list[Section], chunks: list[Chunk], progress_path: Path | None):
        self.title = title
        self.sections = sections
        self.chunks = chunks
        self.progress_path = progress_path
        self.progress = Progress.load(progress_path) if progress_path else Progress()
        if self.progress.cursor >= len(chunks):  # a stale file from an older index
            self.progress.cursor = -1
        self._by_id = {s.section_id: s for s in sections}
        # Each section's OWN chunk range (excluding children), for "part k of n".
        self._own: dict[str, tuple[int, int]] = {}
        for c in chunks:
            first, _ = self._own.get(c.section_id, (c.order, c.order))
            self._own[c.section_id] = (first, c.order)

    # ---- resolving targets -------------------------------------------------------------

    def resolve(self, target: str) -> int:
        t = (target or "next").strip().lower()
        last = len(self.chunks) - 1
        if last < 0:
            raise LessonError("The document has no readable text.")
        cursor = self.progress.cursor
        if t in ("next", "continue"):
            if cursor >= last:
                raise LessonError("We've reached the end of the document.")
            return cursor + 1
        if t in ("current", "again", "repeat"):
            return max(cursor, 0)
        if t in ("previous", "prev", "back"):
            return max(cursor - 1, 0)
        if t in ("start", "beginning", "first"):
            return 0
        if m := _PAGE.match(t):
            return self._order_for_page(int(m.group(1)))
        if _SECTION_ID.match(t):
            return self._order_for_section(self._by_id.get(t), target)
        return self._order_for_section(self._find_by_title(t), target)

    def _order_for_page(self, page: int) -> int:
        for c in self.chunks:
            if c.page_start <= page <= c.page_end or c.page_start > page:
                return c.order
        raise LessonError(f"Page {page} is past the end of the document.")

    def _order_for_section(self, section: Section | None, target: str) -> int:
        if section is None:
            raise LessonError(
                f"No section matches '{target}'. Use get_outline to see sections, or search_document."
            )
        if section.first_order < 0:
            raise LessonError(f"Section '{section.title}' has no text to read.")
        return section.first_order

    def _find_by_title(self, query: str) -> Section | None:
        # "chapter 3" / "section 3.1" / "3.1" -> the section numbered exactly that.
        if m := _NUMBER_REF.match(query):
            number = m.group(1)
            for s in self.sections:
                lead = _LEADING_NUMBER.match(s.title.lower())
                if lead and lead.group(1) == number and s.first_order >= 0:
                    return s
        candidates = [s for s in self.sections if query in s.title.lower() and s.first_order >= 0]
        # Prefer the shallowest match ("Chapter 3" over a subsection that mentions it).
        return min(candidates, key=lambda s: (s.level, s.first_order)) if candidates else None

    # ---- tool-facing operations --------------------------------------------------------

    def read(self, target: str = "next") -> dict:
        order = self.resolve(target)
        chunk = self.chunks[order]
        section = self._by_id[chunk.section_id]
        own_first, own_last = self._own[chunk.section_id]

        self.progress.cursor = order
        self.progress.last_summary = (
            f"Last read: {chunk.heading_path or section.title}, page {_pages(chunk.page_start, chunk.page_end)}."
        )
        self._save()

        return {
            "text": chunk.body,
            "section_id": section.section_id,
            "section": chunk.heading_path or section.title,
            "pages": _pages(chunk.page_start, chunk.page_end),
            "position": f"{section.title}, part {order - own_first + 1} of {own_last - own_first + 1}",
            "is_section_end": order == own_last,
            "is_document_end": order == len(self.chunks) - 1,
        }

    def _save(self) -> None:
        self.progress.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if not self.progress_path:
            return
        try:
            self.progress.save(self.progress_path)
        except OSError as e:
            # The position is kept in memory and every save writes all of it, so the next change saves this one too.
            # Teaching must not fail because a file was briefly locked.
            log.warning("Couldn't save the reading position to %s (%s); it will be saved with the next change.",
                        self.progress_path, e)

    def position(self) -> dict:
        cursor = self.progress.cursor
        if cursor < 0:
            return {"started": False}
        chunk = self.chunks[cursor]
        section = self._by_id[chunk.section_id]
        own_first, own_last = self._own[chunk.section_id]
        return {
            "started": True,
            "section_id": section.section_id,
            "section": chunk.heading_path or section.title,
            "pages": _pages(chunk.page_start, chunk.page_end),
            "part": f"{cursor - own_first + 1} of {own_last - own_first + 1}",
        }

    def outline(self, max_level: int = 2) -> dict:
        top = min((s.level for s in self.sections), default=1)
        return {
            "title": self.title,
            "pages": max((c.page_end for c in self.chunks), default=0),
            "sections": [
                {
                    "id": s.section_id,
                    "title": s.title,
                    "level": s.level - top + 1,
                    "pages": _pages(s.page_start, s.page_end),
                }
                for s in self.sections
                if s.level - top + 1 <= max_level and s.first_order >= 0
            ],
            "position": self.position(),
        }
