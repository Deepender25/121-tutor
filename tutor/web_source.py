"""Add a source from a link: a PDF file on the web, or a web page read with TinyFish Fetch.

A link straight to a PDF is downloaded as it is (any WEB_SEARCH setting). A web page needs WEB_SEARCH=tinyfish:
TinyFish Fetch opens it in a real browser (scripts run, cookie banners and menus dropped) and returns clean HTML,
which PyMuPDF lays out as a PDF whose outline is the page's headings. From there it's an ordinary PDF source, so
outline, search, the page viewer and page citations all work unchanged; its first lines name the original address.

The server downloads only from public addresses (no localhost, private networks or cloud metadata), checked again on
every redirect: on a hosted server, a link must not be a way to reach the machine's own services.
"""

import html as htmllib
import io
import ipaddress
import itertools
import logging
import re
import socket
from datetime import date
from urllib.parse import urljoin, urlparse

import httpx
import pymupdf

from tutor import web

log = logging.getLogger("web")

MAX_REDIRECTS = 5
MAX_PAGES = 400  # a web page laid out longer than this is almost certainly not study material

FETCH_ERRORS = {  # TinyFish Fetch's per-page error codes (docs.tinyfish.ai/fetch-api/reference)
    "bot_blocked": "The site blocked automated reading of this page.",
    "login_required": "That page needs a login, so it can't be added.",
    "page_not_found": "That page doesn't exist (404).",
    "target_unreachable": "The site couldn't be reached.",
    "timeout": "The page took too long to load.",
    "empty_content": "The page has no readable text.",
    "content_too_large": "The page is too large to add.",
    "invalid_url": "That address can't be read.",
    "invalid_redirect_url": "That address redirects somewhere that can't be read.",
    "target_http_error": "The site returned an error for this page.",
    "proxy_error": "The page couldn't be reached right now.",
}

_DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|template|svg|math|iframe|video|audio|canvas|object|select|textarea|button|nav)\b"
    r".*?</\1\s*>", re.S | re.I)
_DROP_TAGS = re.compile(r"<(img|picture|source|input|embed|link|meta|base|track|param|wbr)\b[^>]*>", re.I)
_TAG = re.compile(r"<[a-zA-Z][^>]*>")
_ATTRS = re.compile(r"""\s(style|class|id|on\w+)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_CSS = """
body { font-family: sans-serif; font-size: 11pt; line-height: 1.45; }
h1 { font-size: 20pt; font-weight: bold; margin: 0 0 6pt 0; }
h2 { font-size: 15pt; font-weight: bold; margin: 14pt 0 4pt 0; }
h3 { font-size: 13pt; font-weight: bold; margin: 10pt 0 3pt 0; }
h4, h5, h6 { font-size: 11.5pt; font-weight: bold; margin: 8pt 0 2pt 0; }
p, li { margin: 0 0 6pt 0; }
pre, code { font-family: monospace; font-size: 9pt; }
table { border-collapse: collapse; }
td, th { border: 0.5pt solid #999; padding: 2pt 4pt; font-size: 9.5pt; }
.source { font-size: 9pt; color: #555; margin-bottom: 12pt; }
"""


class LinkError(RuntimeError):
    """The link can't be added; the message is fit to show."""


def check_public(url: str) -> str:
    """The URL, if it's http(s) on a host that resolves only to public addresses; LinkError otherwise."""
    url = (url or "").strip()
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise LinkError("Paste a full web address, starting with http:// or https://.")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise LinkError(f"Couldn't find the site {parts.hostname}. Check the address.") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise LinkError("That address points to a private network or this computer, so it can't be added.")
    return url


def download_pdf(url: str, limit_bytes: int) -> bytes | None:
    """The file's bytes when the link is a PDF; None when it's something else (a web page). Follows redirects
    itself so every hop gets the public-address check."""
    for _ in range(MAX_REDIRECTS + 1):
        check_public(url)
        with httpx.stream("GET", url, follow_redirects=False, timeout=httpx.Timeout(30, connect=10),
                          headers={"User-Agent": "Mozilla/5.0 (VoiceTutor; +add-source)"}) as r:
            if r.is_redirect:
                url = urljoin(url, r.headers.get("location", ""))
                continue
            if r.status_code >= 400:
                # A web page may refuse this plain request (Wikipedia answers 403) yet open in TinyFish's real
                # browser, so only a link that names a PDF file fails here.
                if urlparse(url).path.lower().endswith(".pdf"):
                    raise LinkError(f"The site returned an error for this PDF (HTTP {r.status_code}).")
                return None
            kind = r.headers.get("content-type", "").lower()
            chunks = r.iter_bytes(64 * 1024)
            first = next(chunks, b"")
            if not first.startswith(b"%PDF-"):
                if "pdf" in kind:
                    raise LinkError("The link says it's a PDF but the file isn't a valid PDF.")
                return None  # a web page (or anything else): for TinyFish to read
            data = bytearray()
            for chunk in itertools.chain([first], chunks):  # the first chunk counts towards the limit too
                data += chunk
                if len(data) > limit_bytes:
                    raise LinkError(f"That PDF is larger than {max(1, limit_bytes // (1024 * 1024))} MB.")
            return bytes(data)
    raise LinkError("That address redirects too many times.")


def _clean_html(body: str) -> str:
    body = _DROP_BLOCKS.sub("", body)
    body = _DROP_TAGS.sub("", body)
    return _TAG.sub(lambda m: _ATTRS.sub("", m.group(0)), body)  # inside tags only, never the page's text


def html_to_pdf(body: str, title: str, source_url: str) -> bytes:
    """Lay out a page's HTML as an A4 PDF whose outline (bookmarks) is the page's headings."""
    body = _clean_html(body)
    head = f'<p class="source">Source: {htmllib.escape(source_url)} (read {date.today().isoformat()})</p>'
    if not re.search(r"<h1\b", body, re.I):
        head = f"<h1>{htmllib.escape(title)}</h1>" + head
    story = pymupdf.Story(html=head + body, user_css=_CSS)
    out = io.BytesIO()
    writer = pymupdf.DocumentWriter(out)
    page_box = pymupdf.paper_rect("a4")
    where = page_box + (50, 50, -50, -50)
    headings: list[tuple[int, str, int]] = []

    def record(pos):
        if pos.heading and (pos.open_close & 1) and (pos.text or "").strip():
            headings.append((pos.heading, " ".join(pos.text.split()), pos.page))

    page, more = 0, 1
    while more:
        page += 1
        if page > MAX_PAGES:
            writer.close()
            raise LinkError(f"The page is too long to add (over {MAX_PAGES} pages).")
        device = writer.begin_page(page_box)
        more, _ = story.place(where)
        story.element_positions(record, {"page": page})
        story.draw(device)
        writer.end_page()
    writer.close()
    doc = pymupdf.open("pdf", out.getvalue())
    toc, level = [], 0
    for lvl, text, pg in headings:
        level = min(lvl, level + 1) if toc else 1  # a bookmark can't skip levels
        toc.append([level, text[:200], pg])
    if toc:
        doc.set_toc(toc)
    doc.set_metadata({"title": title, "subject": source_url, "creator": "Voice Tutor (TinyFish Fetch)"})
    return doc.tobytes(garbage=3, deflate=True)


def page_to_pdf(url: str) -> tuple[bytes, str]:
    """(pdf bytes, title) for a web page, read with TinyFish Fetch."""
    name, problem = web.choice()
    if problem or name != "tinyfish":
        raise LinkError("Adding web pages uses TinyFish: set WEB_SEARCH=tinyfish and TINYFISH_API_KEY in .env, then "
                        "restart. Links straight to a PDF file work without it.")
    try:
        results, errors = web.tinyfish_fetch([url], web._key("TINYFISH_API_KEY"), fmt="html",
                                             purpose=web.SOURCE_PURPOSE, per_page_ms=60_000)
    except web.WebError as e:
        raise LinkError(str(e)) from None
    except httpx.HTTPError as e:
        raise LinkError(f"Couldn't reach TinyFish: {e.__class__.__name__}.") from None
    if errors and not results:
        code = errors[0].get("error", "")
        raise LinkError(FETCH_ERRORS.get(code, f"TinyFish couldn't read the page ({code or 'unknown error'})."))
    page = results[0] if results else {}
    body = page.get("text")
    if not isinstance(body, str) or not body.strip():
        raise LinkError(FETCH_ERRORS["empty_content"])
    final = page.get("final_url") or url
    title = (page.get("title") or "").strip() or urlparse(final).hostname or "Web page"
    log.info("TinyFish Fetch read %s (%d characters of HTML)", final, len(body))
    try:
        return html_to_pdf(body, title, final), title
    except LinkError:
        raise
    except Exception:
        log.exception("Couldn't lay out %s as a PDF", final)
        raise LinkError("The page was read, but couldn't be turned into a document.") from None
