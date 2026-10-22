"""The web: search for the voice model's `web_search` tool, and reading a page for "Add from link".

Which service, chosen with WEB_SEARCH in .env (lowercase):
  tinyfish    TinyFish Search finds the pages, then TinyFish Fetch reads the top ones in a real browser and
              returns them as clean Markdown, so the tutor answers from what the pages say, not one-line snippets.
              Fetch also turns a web page into a notebook source. Needs TINYFISH_API_KEY.
              (docs.tinyfish.ai, 2026-10-22: Search 12,000 and Fetch 1,000 free pages a day, then wallet credits.)
  tavily      Tavily search, snippets. Needs TAVILY_API_KEY.
  duckduckgo  DuckDuckGo via `ddgs`, snippets. Free, no key.
Not set: Tavily when TAVILY_API_KEY is set, otherwise DuckDuckGo (what the app did before WEB_SEARCH existed).
Gemini's built-in google_search has zero free-tier quota (verified 2026-10-10), so search runs here.

If the chosen service fails, the next one that's available answers (TinyFish, then Tavily, then DuckDuckGo) and
the result says which one did, so the tutor never credits results to a service that didn't produce them.
"""

import logging
import os
import re
import time
from urllib.parse import urlparse

import httpx

log = logging.getLogger("web")

PROVIDERS = ("tinyfish", "tavily", "duckduckgo")
NAMES = {"tinyfish": "TinyFish", "tavily": "Tavily", "duckduckgo": "DuckDuckGo"}
KEYS = {"tinyfish": "TINYFISH_API_KEY", "tavily": "TAVILY_API_KEY"}

TINYFISH_SEARCH = "https://api.search.tinyfish.ai"
TINYFISH_FETCH = "https://api.fetch.tinyfish.ai"
# Told to TinyFish with every request ("purpose": better ranking for the intent behind a terse query).
SEARCH_PURPOSE = "Answer a student's question during a one-to-one tutoring session; the answer will cite these pages."
SOURCE_PURPOSE = "Add this page to a student's study notebook, to be taught from and cited."
FETCH_PAGE_MS = 12_000  # per page, while the person waits for a spoken answer (TinyFish allows up to 110 s)
EXCERPT_CHARS = 1_400  # of each fetched page given to the model: its most relevant paragraphs


class WebError(RuntimeError):
    """A web service failed, with a message fit to show (no response bodies or keys)."""


def _key(name: str) -> str:
    return os.environ.get(name, "").strip()


def choice() -> tuple[str, str | None]:
    """(provider, problem). `problem` explains a WEB_SEARCH value that can't be used: unknown, or its key missing.
    Read on every call, like the keys, so a test or a changed environment takes effect without a restart."""
    raw = os.environ.get("WEB_SEARCH", "").strip()
    value = raw.lower()
    if not value:
        return ("tavily" if _key("TAVILY_API_KEY") else "duckduckgo"), None
    if value not in PROVIDERS:
        return value, f"WEB_SEARCH must be tinyfish, tavily or duckduckgo (it is '{raw}'). Fix it in .env."
    if value in KEYS and not _key(KEYS[value]):
        return value, f"WEB_SEARCH={value} needs {KEYS[value]} in .env."
    return value, None


def provider() -> str:
    return choice()[0]


def describe() -> str:
    """For the start-up banner and Settings."""
    name, problem = choice()
    if problem:
        return problem
    if name == "tinyfish":
        return "TinyFish (Search, and Fetch reads the top pages)"
    return NAMES[name] + ("" if name == "tavily" or _key("TAVILY_API_KEY") else " (set WEB_SEARCH=tinyfish or add TAVILY_API_KEY for better results)")


def _site(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


# ---- TinyFish ------------------------------------------------------------------------------------------

def _tinyfish_error(r: httpx.Response, what: str) -> WebError:
    code = ""
    try:
        err = r.json().get("error")
        code = err.get("code", "") if isinstance(err, dict) else ""
    except ValueError:
        pass
    reason = {
        401: "it didn't accept the API key (check TINYFISH_API_KEY)",
        402: "today's free allowance is used up and the TinyFish wallet is empty",
        429: "too many requests right now",
    }.get(r.status_code, f"it answered HTTP {r.status_code}")
    return WebError(f"TinyFish {what} failed: {reason}" + (f" [{code}]" if code else ""))


def _tinyfish_search(query: str, max_results: int, key: str) -> list[dict]:
    r = httpx.get(TINYFISH_SEARCH, params={"query": query, "purpose": SEARCH_PURPOSE},
                  headers={"X-API-Key": key}, timeout=10)
    if r.status_code >= 400:
        raise _tinyfish_error(r, "Search")
    return [
        {"title": x.get("title") or "", "url": x.get("url") or "", "snippet": x.get("snippet") or "",
         "site": x.get("site_name") or "", "date": x.get("date") or ""}
        for x in r.json().get("results", [])
    ][:max_results]


def tinyfish_fetch(urls: list[str], key: str, fmt: str = "markdown", purpose: str = SEARCH_PURPOSE,
                   per_page_ms: int = FETCH_PAGE_MS) -> tuple[list[dict], list[dict]]:
    """(results, errors) as TinyFish Fetch returns them: each page read in a real browser (scripts run), as clean
    Markdown or HTML. Per-page failures (blocked, login wall, timeout) come back in errors, not as an exception."""
    r = httpx.post(
        TINYFISH_FETCH,
        json={"urls": urls[:10], "format": fmt, "purpose": purpose, "per_url_timeout_ms": per_page_ms},
        headers={"X-API-Key": key},
        timeout=per_page_ms / 1000 + 15,
    )
    if r.status_code >= 400:
        raise _tinyfish_error(r, "Fetch")
    data = r.json()
    return data.get("results") or [], data.get("errors") or []


_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_WORD = re.compile(r"[^\W_]{3,}")
_STOP = set("the and for are was were with that this from what which who whom how why when where does did can "
            "could would should about into than then them they their there these those your you our its has have "
            "had not but all any some more most other also just only".split())


def excerpt(markdown: str, query: str, limit: int = EXCERPT_CHARS) -> str:
    """The page's paragraphs that share the most words with the question, kept in page order, up to `limit`
    characters; the opening of the page when nothing matches. Images and link targets are dropped."""
    text = _MD_LINK.sub(r"\1", _MD_IMAGE.sub("", markdown or ""))
    paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text)]
    paragraphs = [p for p in paragraphs if len(p) >= 40 or p.startswith("#")]
    if not paragraphs:
        return ""
    terms = {w for w in _WORD.findall(query.lower()) if w not in _STOP}
    scored = [(sum(1 for w in set(_WORD.findall(p.lower())) if w in terms), i) for i, p in enumerate(paragraphs)]
    picked, used = [], 0
    for score, i in sorted(scored, key=lambda s: (-s[0], s[1])):
        if score == 0 and picked:
            break
        if used + len(paragraphs[i]) > limit and picked:
            continue
        picked.append(i)
        used += len(paragraphs[i])
        if used >= limit:
            break
    if not any(score for score, _ in scored):  # nothing matched: the start of the page
        picked, used = [], 0
        for i, p in enumerate(paragraphs):
            if used + len(p) > limit and picked:
                break
            picked.append(i)
            used += len(p)
    out = "\n\n".join(paragraphs[i] for i in sorted(picked))
    return out[:limit].rstrip() + ("…" if len(out) > limit else "")


def _read_pages(results: list[dict], query: str, key: str, pages: int) -> tuple[int, str | None]:
    """Fills in `content` for the top results with TinyFish Fetch. (pages read, problem or None)."""
    urls = [r["url"] for r in results[:pages]]
    if not urls:
        return 0, None
    try:
        fetched, errors = tinyfish_fetch(urls, key)
    except (WebError, httpx.HTTPError) as e:
        return 0, str(e)
    by_url = {}
    for page in fetched:
        for u in (page.get("url"), page.get("final_url")):
            if u:
                by_url[u] = page
    read = 0
    for r in results[:pages]:
        page = by_url.get(r["url"])
        text = page.get("text") if page else None
        if isinstance(text, str) and text.strip():
            r["content"] = excerpt(text, query)
            read += bool(r["content"])
    if errors:
        log.info("TinyFish Fetch couldn't read %d of %d pages: %s", len(errors), len(urls),
                 ", ".join(f"{_site(e.get('url', ''))} ({e.get('error', '?')})" for e in errors[:5]))
    return read, None


# ---- Tavily, DuckDuckGo ----------------------------------------------------------------------------------

def _tavily(query: str, max_results: int, key: str) -> list[dict]:
    r = httpx.post(
        "https://api.tavily.com/search",
        json={"query": query, "max_results": max_results, "search_depth": "basic"},
        headers={"Authorization": f"Bearer {key}"},
        timeout=15,
    )
    r.raise_for_status()
    return [
        {"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
        for x in r.json().get("results", [])
    ]


def _duckduckgo(query: str, max_results: int) -> list[dict]:
    from ddgs import DDGS

    return [
        {"title": x.get("title", ""), "url": x.get("href", ""), "snippet": x.get("body", "")}
        for x in DDGS(timeout=10).text(query, max_results=max_results)
    ]


# ---- the tool ------------------------------------------------------------------------------------------

def _chain(first: str) -> list[str]:
    """The chosen service, then the others after it that can run (have their key)."""
    rest = PROVIDERS[PROVIDERS.index(first):]
    return [p for p in rest if p not in KEYS or _key(KEYS[p])]


def fetch_pages() -> int:
    """How many of the top results TinyFish Fetch reads per search (TINYFISH_FETCH_PAGES, 0-5; 0 = snippets only)."""
    try:
        return max(0, min(5, int(os.environ.get("TINYFISH_FETCH_PAGES", "3"))))
    except ValueError:
        return 3


def web_search(query: str, max_results: int = 5) -> dict:
    max_results = max(1, min(int(max_results or 5), 8))
    first, problem = choice()
    if problem:
        log.warning("Web search unavailable: %s", problem)
        return {"error": f"Web search isn't set up: {problem}"}
    started = time.perf_counter()
    failures: list[str] = []
    for name in _chain(first):
        try:
            if name == "tinyfish":
                results = _tinyfish_search(query, max_results, _key("TINYFISH_API_KEY"))
            elif name == "tavily":
                results = _tavily(query, max_results, _key("TAVILY_API_KEY"))
            else:
                results = _duckduckgo(query, max_results)
        except Exception as e:  # network errors, limits: try the next service, never crash the session
            reason = str(e) if isinstance(e, WebError) else f"{NAMES[name]} failed: {e.__class__.__name__}: {e}"
            log.warning("Web search: %s", reason)
            failures.append(reason)
            continue
        results = [
            {"title": r["title"], "site": r.get("site") or _site(r["url"]), "url": r["url"],
             "snippet": (r.get("snippet") or "")[:600], **({"date": r["date"]} if r.get("date") else {})}
            for r in results if r.get("url")
        ]
        note = []
        if failures:
            note.append(f"{' '.join(failures)} These results are from {NAMES[name]}.")
        read = 0
        if name == "tinyfish" and results and fetch_pages():
            read, fetch_problem = _read_pages(results, query, _key("TINYFISH_API_KEY"), fetch_pages())
            if fetch_problem:
                log.warning("Web search: %s; answering from the search snippets", fetch_problem)
                note.append("The pages themselves couldn't be read, so these are search snippets only.")
        log.info("Web search via %s: %d results%s in %.1fs", NAMES[name], len(results),
                 f", {read} pages read" if name == "tinyfish" else "", time.perf_counter() - started)
        out = {"searched_with": NAMES[name], "results": results}
        if read:
            out["about_content"] = ("'content' is text from the page itself (read with TinyFish Fetch): prefer it "
                                    "over the snippet, and name the site you take it from.")
        if not results:
            note.append("No web results.")
        if note:
            out["note"] = " ".join(note)
        return out
    return {"error": "Web search failed: " + " ".join(failures)}
