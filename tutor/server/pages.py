"""Render PDF pages to PNG (cached on disk) and locate passage text on a page for highlighting."""

import json
import math
from collections import OrderedDict
import re
import threading
import unicodedata
from pathlib import Path

import pymupdf

from tutor.document import strip_tags

_render_lock = threading.Lock()  # MuPDF documents aren't thread-safe; renders are fast enough to serialize
_MARKDOWN = re.compile(r"[*_`#>|]+|\[|\]\([^)]*\)")


def render_page(pdf: Path, page: int, scale: float, cache_dir: Path) -> bytes:
    if not math.isfinite(scale):  # ?scale=nan or inf: min/max would keep nan
        scale = 1.6
    scale = round(min(max(scale, 0.75), 3.0), 2)
    cached = cache_dir / f"{page}@{scale}.png"
    if cached.exists():
        return cached.read_bytes()
    with _render_lock, pymupdf.open(str(pdf)) as doc:
        if not 1 <= page <= doc.page_count:
            raise IndexError(page)
        png = doc[page - 1].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(png)
    return png


def page_sizes(pdf: Path, cache_dir: Path) -> list[list[float]]:
    """[width, height] of every page in points (as rendered, rotation applied); cached next to the renders.
    The viewer lays out the whole document from these before any page image has loaded."""
    cached = cache_dir / "sizes.json"
    if cached.exists():
        try:
            return json.loads(cached.read_text(encoding="utf-8"))
        except ValueError:
            pass
    with _render_lock, pymupdf.open(str(pdf)) as doc:
        sizes = [[round(p.rect.width, 2), round(p.rect.height, 2)] for p in doc]
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(sizes), encoding="utf-8")
    return sizes


def page_count(pdf: Path) -> int:
    with _render_lock, pymupdf.open(str(pdf)) as doc:
        return doc.page_count


def find_text(pdf: Path, page: int, text: str, max_phrases: int = 4) -> list[list[float]]:
    """Rectangles (as fractions of page width/height) covering the start of `text` on the page.
    Searches short phrases because exact multi-line matches break on hyphenation and markup."""
    clean = " ".join(_MARKDOWN.sub(" ", text).split())
    words = clean.split()
    phrases = [" ".join(words[i:i + 6]) for i in range(0, min(len(words), 6 * max_phrases), 6)]
    rects: list[list[float]] = []
    with _render_lock, pymupdf.open(str(pdf)) as doc:
        if not 1 <= page <= doc.page_count:
            return []
        p = doc[page - 1]
        w, h = p.rect.width, p.rect.height
        for phrase in phrases:
            if len(phrase) < 12:
                continue
            for r in p.search_for(phrase):
                rects.append([round(r.x0 / w, 4), round(r.y0 / h, 4), round(r.x1 / w, 4), round(r.y1 / h, 4)])
    return rects


# ---- locating a sentence, to highlight a cited passage ----------------------------------------

_TOKEN = re.compile(r"[^\W_]+")


def _tokens(text: str) -> list[str]:
    # NFKC folds ligatures (ﬁ, ﬀ) and full-width forms, so PDF text and chunk text tokenize alike
    return _TOKEN.findall(unicodedata.normalize("NFKC", text).lower())


def _page_words(page) -> list[tuple[str, tuple]]:
    """(token, (x0, y0, x1, y1, block, line)) per word, with end-of-line hyphenation joined back."""
    out: list[tuple[str, tuple]] = []
    words = page.get_text("words")  # natural block order: table cells stay whole (sort=True interleaves them)
    i = 0
    while i < len(words):
        x0, y0, x1, y1, word, block, line, _ = words[i]
        box = (x0, y0, x1, y1, block, line)
        if word.endswith("-") and i + 1 < len(words) and (words[i + 1][5], words[i + 1][6]) != (block, line):
            joined = "".join(_tokens(word)) + "".join(_tokens(words[i + 1][4]))
            nxt = words[i + 1]
            out.append((joined, box))
            out.append(("", (nxt[0], nxt[1], nxt[2], nxt[3], nxt[5], nxt[6])))  # keeps its box for the line rect
            i += 2
            continue
        for tok in _tokens(word) or [""]:
            out.append((tok, box))
        i += 1
    return out


_words_cache: OrderedDict = OrderedDict()  # (pdf, mtime, page) -> words; citations often hit the same pages


def _cached_words(pdf: Path, number: int, page) -> list[tuple[str, tuple]]:
    key = (str(pdf), pdf.stat().st_mtime_ns, number)
    if key in _words_cache:
        _words_cache.move_to_end(key)
        return _words_cache[key]
    words = _words_cache[key] = _page_words(page)
    if len(_words_cache) > 96:
        _words_cache.popitem(last=False)
    return words


def _align(target: list[str], page_toks: list[str], max_skip: int = 4) -> tuple[int, int, int]:
    """Best in-order fuzzy match of `target` inside `page_toks`: (matched, first_index, last_index)."""
    best = (0, -1, -1)
    starts = set(target[:4])
    for i, tok in enumerate(page_toks):
        if tok not in starts:
            continue
        t = target.index(tok)
        matched, k, first, last = 0, i, i, i
        while t < len(target) and k < len(page_toks):
            # look a few tokens ahead on either side for the next match (skips markup, hyphenation, typos)
            found = None
            for dt in range(0, max_skip):
                if t + dt >= len(target):
                    break
                for dk in range(0, max_skip):
                    if k + dk < len(page_toks) and page_toks[k + dk] == target[t + dt]:
                        found = (dt, dk)
                        break
                if found:
                    break
            if not found:
                break
            t += found[0] + 1
            k += found[1] + 1
            matched += 1
            last = k - 1
        # tighter spans win ties (a stray word in a heading above shouldn't drag the start upwards)
        if matched and (matched, first - last) > (best[0], best[1] - best[2]):
            best = (matched, first, last)
    return best


def locate_text(pdf: Path, text: str, first_page: int, last_page: int) -> dict:
    """Find where `text` (a sentence from a chunk, possibly markdown) is printed, searching first..last page
    (plus one page either side). Returns {"page", "rects"} with one rectangle per printed line, as fractions
    of the page, or {"page": None, "rects": []} if it can't be found confidently."""
    target = _tokens(_MARKDOWN.sub(" ", strip_tags(text)))[:60]
    if len(target) < 3:
        return {"page": None, "rects": []}
    best = None
    with _render_lock, pymupdf.open(str(pdf)) as doc:
        lo, hi = max(1, first_page - 1), min(doc.page_count, max(first_page, last_page) + 1)
        for number in range(lo, hi + 1):
            # prefer the pages the chunk says it's on: neighbours only win with a clearly better match
            bias = 0 if first_page <= number <= last_page else -2
            page = doc[number - 1]
            words = _cached_words(pdf, number, page)
            matched, a, b = _align(target, [w[0] for w in words])
            if matched and (best is None or matched + bias > best[0]):
                best = (matched + bias, matched, number, page.rect.width, page.rect.height, words[a:b + 1])
    if best is None or best[1] < max(3, int(len(target) * 0.45)):
        return {"page": None, "rects": []}
    _, _, number, w, h, span = best
    lines: dict[tuple, list[float]] = {}
    for _, (x0, y0, x1, y1, block, line) in span:
        r = lines.setdefault((block, line), [x0, y0, x1, y1])
        r[0], r[1], r[2], r[3] = min(r[0], x0), min(r[1], y0), max(r[2], x1), max(r[3], y1)
    rects = [[round((x0 - 1) / w, 4), round((y0 - 1) / h, 4), round((x1 + 1) / w, 4), round((y1 + 1) / h, 4)]
             for x0, y0, x1, y1 in sorted(lines.values(), key=lambda r: (r[1], r[0]))]
    return {"page": number, "rects": rects}
