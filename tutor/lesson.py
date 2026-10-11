"""Reading cursor + progress over a document's ordered chunks.

Position lives here, not in the voice model's memory: context compression drops old turns, so the
model is told where we are on every (re)connect and moves through the document only via read().

Completion is tracked per chunk: a chunk is finished when it was read (read_passage) or marked complete
(by the person in the outline, or by the tutor with update_progress). Everything else is derived from
that, so it can never disagree with itself:
  - a section is "completed" when every chunk in it and its subsections is finished, "in_progress" when
    some are, otherwise "not_started" (parents roll up automatically);
  - the document's percentage is the share of its content (chunks) that is finished.
Marking a section incomplete clears its chunks (read or marked), so it really shows as not done.
"""

import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from tutor.document import Chunk, Section

log = logging.getLogger("lesson")
_save_lock = threading.Lock()  # tool calls run in worker threads: one progress write at a time


class LessonError(ValueError):
    """A target that can't be resolved. The message is written to be relayed by the model."""


@dataclass
class Progress:
    cursor: int = -1  # order of the chunk last read; -1 = not started
    read: set[int] = field(default_factory=set)  # chunks read with read_passage
    done: set[int] = field(default_factory=set)  # chunks marked complete (outline or update_progress)
    last_summary: str = ""
    updated_at: str = ""

    @classmethod
    def load(cls, path: Path) -> "Progress":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data["cursor"], set(data["read"]), set(data.get("done", [])),
                   data.get("last_summary", ""), data.get("updated_at", ""))

    @property
    def finished(self) -> set[int]:
        return self.read | self.done

    def save(self, path: Path) -> None:
        data = {
            "cursor": self.cursor,
            "read": sorted(self.read),
            "done": sorted(self.done),
            "last_summary": self.last_summary,
            "updated_at": self.updated_at,
        }
        with _save_lock:
            # Write a temp file, then swap it in, so a crash mid-write never leaves a half-written file.
            fd, tmp = tempfile.mkstemp(prefix="progress-", suffix=".tmp", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=1)
                _replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise


def _replace(src: str, dst: Path, attempts: int = 8) -> None:
    """os.replace, retried briefly on Windows: antivirus scanners and the search indexer open freshly written
    files for a moment, and replacing a file that's open elsewhere fails with WinError 32 / 5."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))  # 50 ms, 100 ms, ... about 1.4 s in all


def _pages(start: int, end: int) -> str:
    return str(start) if start == end else f"{start}-{end}"


_PAGE = re.compile(r"^page[\s:]*(\d+)$", re.IGNORECASE)
_WHOLE = {"all", "everything", "document", "the whole document", "whole document", "the document", "entire document"}
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
        self.progress.read.add(order)
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
            "document_covered": f"{self.percent_covered()}%",
        }

    def reset(self) -> None:
        """Forget progress: back to 'not started', nothing covered."""
        self.progress = Progress()
        self._save()

    def _save(self) -> None:
        self.progress.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if not self.progress_path:
            return
        try:
            self.progress.save(self.progress_path)
        except OSError as e:
            # Progress is kept in memory and every save writes all of it, so the next change saves this one too.
            # Teaching must not fail because a file was briefly locked.
            log.warning("Couldn't save reading progress to %s (%s); it will be saved with the next change.",
                        self.progress_path, e)

    # ---- completion ----------------------------------------------------------------------

    @staticmethod
    def _subtree(section: Section) -> range:
        """Chunk orders of a section and all its subsections."""
        return range(section.first_order, section.last_order + 1) if section.first_order >= 0 else range(0)

    def resolve_section(self, target: str) -> Section:
        """A section by id ('s012'), number ('14', 'chapter 3'), words from its title, or a page ('page:40')."""
        t = (target or "").strip().lower()
        if not t:
            raise LessonError("Say which section: its number, title or id from the outline.")
        if _SECTION_ID.match(t) and t in self._by_id:
            return self._by_id[t]
        if m := _PAGE.match(t):
            chunk = self.chunks[self._order_for_page(int(m.group(1)))]
            return self._by_id[chunk.section_id]
        section = self._find_by_title(t)
        if section is None:
            raise LessonError(f"No section matches '{target}'. Use get_outline to see the sections and their ids.")
        return section

    def status(self, section: Section) -> str:
        orders = self._subtree(section)
        if not orders:
            return "not_started"
        finished = self.progress.finished
        hits = sum(1 for o in orders if o in finished)
        return "completed" if hits == len(orders) else "in_progress" if hits else "not_started"

    def mark(self, target: str, completed: bool = True, through: bool = False) -> dict:
        """Mark a section (with its subsections), everything up to and including it (through), or the whole
        document ('all') as completed or not completed."""
        last = len(self.chunks) - 1
        if last < 0:
            raise LessonError("The document has no readable text.")
        if (target or "").strip().lower() in _WHOLE:
            orders, label = range(0, last + 1), "the whole document"
        else:
            section = self.resolve_section(target)
            if section.first_order < 0:
                raise LessonError(f"Section '{section.title}' has no text to mark.")
            if through:
                orders, label = range(0, section.last_order + 1), f"everything up to and including {section.title}"
            else:
                orders, label = self._subtree(section), section.title
        before = {s.section_id: self.status(s) for s in self.sections if s.first_order >= 0}
        if completed:
            self.progress.done.update(orders)
            # Continue after what was just completed when it reaches the reading position (or nothing was read
            # yet), so "continue" picks up with the first part that isn't done.
            cursor = self.progress.cursor
            if through or cursor < 0 or cursor in orders:
                self.progress.cursor = max(cursor, orders[-1])
        else:
            self.progress.done.difference_update(orders)
            self.progress.read.difference_update(orders)
        self._save()
        changed = [s for s in self.sections if s.first_order >= 0 and self.status(s) != before[s.section_id]]
        total = sum(1 for s in self.sections if s.first_order >= 0)
        done = sum(1 for s in self.sections if s.first_order >= 0 and self.status(s) == "completed")
        return {
            "marked": label,
            "completed": completed,
            "sections_changed": len(changed),
            "sections_completed": f"{done} of {total}",
            "document_covered": f"{self.percent_covered()}%",
        }

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

    def is_covered(self, section: Section) -> bool:
        return self.status(section) == "completed"

    def percent_covered(self) -> int:
        if not self.chunks:
            return 0
        finished = sum(1 for o in self.progress.finished if 0 <= o < len(self.chunks))
        return round(100 * finished / len(self.chunks))

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
                    "status": (status := self.status(s)),
                    "covered": status == "completed",
                }
                for s in self.sections
                if s.level - top + 1 <= max_level and s.first_order >= 0
            ],
            "position": self.position(),
            "document_covered": f"{self.percent_covered()}%",
        }
