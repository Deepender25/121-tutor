"""Web search for the voice model's `web_search` tool.

Gemini's built-in google_search has zero free-tier quota (verified 2026-10-10), so search runs here:
Tavily when TAVILY_API_KEY is set (better snippets, 1,000 free credits/month), otherwise DuckDuckGo
via `ddgs` (free, no key). Results carry title/site/url so the model can attribute out loud and the
UI can show clickable sources.
"""

import os
from urllib.parse import urlparse

import httpx


def _site(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


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


def provider() -> str:
    return "tavily" if os.environ.get("TAVILY_API_KEY", "").strip() else "duckduckgo"


def web_search(query: str, max_results: int = 5) -> dict:
    max_results = max(1, min(int(max_results or 5), 8))
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    try:
        results = _tavily(query, max_results, key) if key else _duckduckgo(query, max_results)
    except Exception as e:  # network errors, rate limits: report, don't crash the session
        if key:  # Tavily failed: fall back once to DuckDuckGo
            try:
                results = _duckduckgo(query, max_results)
            except Exception as e2:
                return {"error": f"Web search failed: {e2}"}
        else:
            return {"error": f"Web search failed: {e}"}
    if not results:
        return {"results": [], "note": "No web results."}
    return {
        "results": [
            {"title": r["title"], "site": _site(r["url"]), "url": r["url"], "snippet": r["snippet"][:600]}
            for r in results
            if r["url"]
        ]
    }
