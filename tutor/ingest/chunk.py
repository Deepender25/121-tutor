"""Split sections into ordered, paragraph-aligned chunks. Chunks never cross section boundaries."""

import re
from typing import Callable

from tutor.document import Chunk, Paragraph, Section

TokenCounter = Callable[[str], int]

# Sentence end: . ! ? or the Devanagari danda, followed by whitespace.
_SENTENCE_END = re.compile(r"(?<=[.!?।])\s+")


def split_sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(text.strip()) if s]


def _split_long(text: str, limit: int, count: TokenCounter) -> list[str]:
    """Break text over `limit` tokens into pieces at sentence (then word) boundaries."""
    pieces: list[str] = []
    current: list[str] = []
    for sentence in split_sentences(text):
        if count(sentence) > limit:
            # A single runaway "sentence" (tables, lists without punctuation): split by words.
            if current:
                pieces.append(" ".join(current))
                current = []
            words, group = sentence.split(), []
            for word in words:
                if group and count(" ".join(group + [word])) > limit:
                    pieces.append(" ".join(group))
                    group = []
                group.append(word)
            if group:
                pieces.append(" ".join(group))
            continue
        if current and count(" ".join(current + [sentence])) > limit:
            pieces.append(" ".join(current))
            current = []
        current.append(sentence)
    if current:
        pieces.append(" ".join(current))
    return pieces


def _tail(text: str, max_tokens: int, count: TokenCounter) -> str:
    """Trailing whole sentences of `text` totalling at most `max_tokens`."""
    picked: list[str] = []
    for sentence in reversed(split_sentences(text)):
        if count(" ".join([sentence] + picked)) > max_tokens:
            break
        picked.insert(0, sentence)
    return " ".join(picked)


def chunk_section(
    section: Section,
    start_order: int,
    count: TokenCounter,
    target: int,
    max_tokens: int,
    overlap: int,
) -> list[Chunk]:
    # 1. Units no larger than max_tokens, each tied to a page.
    units: list[Paragraph] = []
    for para in section.paragraphs:
        if count(para.text) <= max_tokens:
            units.append(para)
        else:
            units.extend(Paragraph(piece, para.page) for piece in _split_long(para.text, target, count))

    # 2. Greedily pack units up to the target size.
    groups: list[list[Paragraph]] = []
    current: list[Paragraph] = []
    current_tokens = 0
    for unit in units:
        tokens = count(unit.text)
        if current and current_tokens + tokens > target:
            groups.append(current)
            current, current_tokens = [], 0
        current.append(unit)
        current_tokens += tokens
    if current:
        groups.append(current)

    # 3. Fold a tiny trailing group into its predecessor when that still fits.
    if len(groups) >= 2:
        last_tokens = sum(count(u.text) for u in groups[-1])
        prev_tokens = sum(count(u.text) for u in groups[-2])
        if last_tokens < target // 4 and prev_tokens + last_tokens <= max_tokens:
            groups[-2].extend(groups.pop())

    # 4. Emit chunks, prefixing each with the tail of the previous one for retrieval context.
    chunks: list[Chunk] = []
    previous_body = ""
    for i, group in enumerate(groups):
        body = "\n\n".join(u.text for u in group)
        prefix = _tail(previous_body, overlap, count) if previous_body and overlap > 0 else ""
        text = f"{prefix}\n\n{body}" if prefix else body
        chunks.append(
            Chunk(
                order=start_order + i,
                section_id=section.section_id,
                heading_path=section.heading_path,
                page_start=min(u.page for u in group),
                page_end=max(u.page for u in group),
                text=text,
                overlap_chars=len(prefix) + 2 if prefix else 0,
            )
        )
        previous_body = body
    return chunks


def chunk_document(
    sections: list[Section],
    count: TokenCounter,
    target: int,
    max_tokens: int,
    overlap: int,
) -> list[Chunk]:
    """Chunk all sections in reading order and fill each section's first/last_order range
    (covering its descendants, which follow it in the list with a deeper level)."""
    chunks: list[Chunk] = []
    own_ranges: list[tuple[int, int]] = []
    for section in sections:
        made = chunk_section(section, len(chunks), count, target, max_tokens, overlap)
        own_ranges.append((made[0].order, made[-1].order) if made else (-1, -1))
        chunks.extend(made)

    for i, section in enumerate(sections):
        orders = [o for o in own_ranges[i] if o >= 0]
        j = i + 1
        while j < len(sections) and sections[j].level > section.level:
            orders.extend(o for o in own_ranges[j] if o >= 0)
            j += 1
        section.first_order = min(orders) if orders else -1
        section.last_order = max(orders) if orders else -1
    return chunks
