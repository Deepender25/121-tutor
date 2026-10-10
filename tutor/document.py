"""Plain data types shared by ingestion, retrieval and the lesson cursor."""

import re
from dataclasses import dataclass, field

_TAGS = re.compile(r"</?[a-zA-Z][^>]*>")


def strip_tags(text: str) -> str:
    """Remove stray HTML tags (pymupdf4llm sometimes emits <mark>, <br> etc. in headings)."""
    return " ".join(_TAGS.sub("", text or "").split())


@dataclass
class Paragraph:
    text: str
    page: int  # 1-based


@dataclass
class Section:
    section_id: str
    level: int
    title: str
    heading_path: str
    page_start: int
    page_end: int
    paragraphs: list[Paragraph] = field(default_factory=list)  # own body only, not children's
    # Inclusive chunk-order range covering this section AND its descendants; -1 if empty.
    first_order: int = -1
    last_order: int = -1


@dataclass
class Chunk:
    order: int  # global reading order; the teaching cursor walks this
    section_id: str
    heading_path: str
    page_start: int
    page_end: int
    text: str  # retrieval text, starts with overlap copied from the previous chunk
    overlap_chars: int = 0  # length of that leading overlap; teaching skips it

    @property
    def body(self) -> str:
        return self.text[self.overlap_chars:]
