"""PDF -> ordered sections with their body paragraphs.

Structure comes from, in order of preference:
  1. the PDF's own outline (bookmarks / TOC),
  2. headings detected by pymupdf4llm's layout analysis,
  3. synthetic sections every few pages.
Running page headers/footers are dropped using pymupdf4llm's box classes.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf
import pymupdf4llm

from tutor.document import Paragraph, Section, strip_tags

SKIP_CLASSES = {"page-header", "page-footer", "picture"}
_HEADING_MARKS = re.compile(r"^#+\s*")
_EMPHASIS = re.compile(r"[*_`]+")
_NON_WORD = re.compile(r"[^\w]+")


@dataclass
class Block:
    kind: str  # "heading" | "text"
    text: str
    page: int
    level: int = 0  # headings only: number of leading '#'


@dataclass
class ParsedDocument:
    title: str
    page_count: int
    sections: list[Section]
    structure_source: str  # "outline" | "headings" | "pages"


def _clean_heading(text: str) -> str:
    return strip_tags(_EMPHASIS.sub("", _HEADING_MARKS.sub("", text)))


def _norm(text: str) -> str:
    return _NON_WORD.sub(" ", _clean_heading(text).lower()).strip()


def extract_blocks(path: Path) -> tuple[dict, list[Block]]:
    pages = pymupdf4llm.to_markdown(str(path), page_chunks=True)
    blocks: list[Block] = []
    with pymupdf.open(str(path)) as doc:
        for page in pages:
            number = page["metadata"]["page_number"]
            text = page["text"]
            for box in page["page_boxes"]:
                cls = box.get("class", "text")
                if cls in SKIP_CLASSES or not box.get("pos"):
                    continue
                start, end = box["pos"]
                content = text[start:end].strip()
                if not _clean_heading(content):
                    # pymupdf4llm occasionally emits an empty box (seen: "### " for a real heading);
                    # fall back to the raw text inside the box.
                    raw = doc[number - 1].get_textbox(pymupdf.Rect(box["bbox"])).strip()
                    if not raw:
                        continue
                    level = len(content) - len(content.lstrip("#"))
                    content = ("#" * level + " " + raw) if level else raw
                if cls == "section-header":
                    level = len(content) - len(content.lstrip("#")) or 1
                    blocks.append(Block("heading", _clean_heading(content), number, level))
                else:
                    # A text box can hold several paragraphs.
                    blocks.extend(
                        Block("text", p.strip(), number) for p in re.split(r"\n\s*\n", content) if p.strip()
                    )
    return (pages[0]["metadata"] if pages else {}), blocks


def _find_heading(blocks: list[Block], start: int, title: str, page: int) -> int | None:
    """Index of the block that is this outline entry's heading, searching forward from `start`."""
    wanted = _norm(title)
    if not wanted:
        return None
    for i in range(start, len(blocks)):
        b = blocks[i]
        if b.page > page + 1:
            break
        if b.page < page - 1:
            continue
        got = _norm(b.text.split("\n", 1)[0])
        if got == wanted or (b.kind == "heading" and (got.startswith(wanted) or (wanted.startswith(got) and len(got) > 3))):
            return i
    return None


def _first_block_on_page(blocks: list[Block], start: int, page: int) -> int:
    for i in range(start, len(blocks)):
        if blocks[i].page >= page:
            return i
    return len(blocks)


def _boundaries_from_outline(blocks: list[Block], toc: list) -> list[tuple[int, int, str, bool]]:
    """(block index, level, title, consumes_block) per outline entry, in order."""
    out = []
    cursor = 0
    for level, title, page in toc:
        if page < 1 or not title.strip():
            continue
        found = _find_heading(blocks, cursor, title, page)
        if found is not None:
            out.append((found, level, title.strip(), True))
            cursor = found + 1
        else:
            index = _first_block_on_page(blocks, cursor, page)
            out.append((index, level, title.strip(), False))
            cursor = index
    return out


_NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+\S")


def _heading_level(block: Block) -> int:
    # Numbering ("2.2 Filters" -> 2) is more reliable than the font-size guess behind '#' counts.
    if m := _NUMBERED.match(block.text):
        return m.group(1).count(".") + 1
    return block.level


def _boundaries_from_headings(blocks: list[Block]) -> list[tuple[int, int, str, bool]]:
    return [(i, _heading_level(b), b.text, True) for i, b in enumerate(blocks) if b.kind == "heading"]


def _boundaries_from_pages(blocks: list[Block], every: int) -> list[tuple[int, int, str, bool]]:
    out, next_page = [], 1
    last_page = blocks[-1].page if blocks else 1
    for i, b in enumerate(blocks):
        if b.page >= next_page:
            end = min(b.page + every - 1, last_page)
            out.append((i, 1, f"Pages {b.page}-{end}" if end > b.page else f"Page {b.page}", False))
            next_page = b.page + every
    return out


def build_sections(blocks: list[Block], boundaries: list[tuple[int, int, str, bool]], doc_title: str) -> list[Section]:
    sections: list[Section] = []
    # Text before the first boundary (title page, abstract, preface without a heading).
    first = boundaries[0][0] if boundaries else len(blocks)
    if any(b.kind == "text" for b in blocks[:first]):
        top = min((lvl for _, lvl, _, _ in boundaries), default=1)
        boundaries = [(0, top, doc_title or "Beginning", False)] + boundaries

    stack: list[tuple[int, str]] = []  # (level, title) for heading paths
    for n, (index, level, title, consumes) in enumerate(boundaries):
        end = boundaries[n + 1][0] if n + 1 < len(boundaries) else len(blocks)
        body_blocks = blocks[index + 1 if consumes else index:end]
        # Headings inside a section that weren't outline entries stay as text (e.g. "Example").
        paragraphs = [Paragraph(b.text, b.page) for b in body_blocks]
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        page_start = blocks[index].page if index < len(blocks) else (blocks[-1].page if blocks else 1)
        sections.append(
            Section(
                section_id=f"s{n:03d}",
                level=level,
                title=title,
                heading_path=" › ".join(t for _, t in stack),
                page_start=page_start,
                page_end=max([page_start] + [p.page for p in paragraphs]),
                paragraphs=paragraphs,
            )
        )

    # A parent's page range spans its children.
    for i, s in enumerate(sections):
        j = i + 1
        while j < len(sections) and sections[j].level > s.level:
            s.page_end = max(s.page_end, sections[j].page_end)
            j += 1
    return sections


def parse_pdf(path: Path, fallback_every: int = 5) -> ParsedDocument:
    metadata, blocks = extract_blocks(path)
    with pymupdf.open(str(path)) as doc:
        toc = doc.get_toc(simple=True)
        page_count = doc.page_count
    title = (metadata.get("title") or "").strip() or path.stem

    if toc:
        boundaries, source = _boundaries_from_outline(blocks, toc), "outline"
    elif any(b.kind == "heading" for b in blocks):
        boundaries, source = _boundaries_from_headings(blocks), "headings"
    else:
        boundaries, source = _boundaries_from_pages(blocks, fallback_every), "pages"

    return ParsedDocument(title, page_count, build_sections(blocks, boundaries, title), source)
