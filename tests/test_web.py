"""Web search: the WEB_SEARCH switch, TinyFish Search + Fetch, and falling back to Tavily / DuckDuckGo.

TinyFish is faked at the HTTP layer with the response shapes from its API reference (docs.tinyfish.ai), so these
run without a key or internet."""

import json

import httpx
import pytest

from tutor import web

KEYS = ("WEB_SEARCH", "TINYFISH_API_KEY", "TAVILY_API_KEY", "TINYFISH_FETCH_PAGES")

PAGE = """# Photosynthesis - Wikipedia

[Jump to content](https://en.wikipedia.org/#content) ![logo](https://upload.example/logo.png)

Main menu · Navigation · Contribute · Search · Donate

Photosynthesis is the process by which plants use light energy to make glucose from carbon dioxide and water,
releasing oxygen. It takes place in the [chloroplast](https://en.wikipedia.org/wiki/Chloroplast).

The Calvin cycle uses the enzyme RuBisCO to fix carbon dioxide into organic molecules in the stroma.

History: Jan Ingenhousz showed in 1779 that light is needed. This paragraph is about history, not enzymes at all.
"""

SEARCH_JSON = {
    "query": "rubisco calvin cycle",
    "results": [
        {"position": 1, "site_name": "en.wikipedia.org", "title": "Photosynthesis", "snippet": "Photosynthesis is...",
         "url": "https://en.wikipedia.org/wiki/Photosynthesis"},
        {"position": 2, "site_name": "khanacademy.org", "title": "The Calvin cycle", "snippet": "In the Calvin cycle...",
         "url": "https://www.khanacademy.org/calvin"},
        {"position": 3, "site_name": "britannica.com", "title": "RuBisCO", "snippet": "RuBisCO is an enzyme...",
         "url": "https://www.britannica.com/rubisco", "date": "2026-03-01"},
        {"position": 4, "site_name": "example.org", "title": "Fourth", "snippet": "...", "url": "https://example.org/4"},
    ],
    "total_results": 4,
    "page": 0,
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)


class FakeHttp:
    """Stands in for httpx.get / httpx.post inside tutor.web and records what was sent."""

    def __init__(self, search=(200, SEARCH_JSON), fetch=None):
        self.search, self.fetch, self.calls = search, fetch, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params, headers, None))
        status, body = self.search
        return httpx.Response(status, json=body, request=httpx.Request("GET", url))

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, None, headers, json))
        if self.fetch is None:
            raise AssertionError("Fetch wasn't expected")
        status, body = self.fetch(json) if callable(self.fetch) else self.fetch
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))


def fetched_pages(body):
    return 200, {
        "results": [{"url": u, "final_url": u, "title": "t", "format": "markdown",
                     "text": PAGE if "wikipedia" in u else "Short page about the Calvin cycle and RuBisCO enzyme in plants."}
                    for u in body["urls"] if "britannica" not in u],
        "errors": [{"url": u, "error": "bot_blocked"} for u in body["urls"] if "britannica" in u],
    }


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        f = FakeHttp(**kw)
        monkeypatch.setattr(web.httpx, "get", f.get)
        monkeypatch.setattr(web.httpx, "post", f.post)
        return f
    return install


# ---- the switch

def test_unset_keeps_the_old_behaviour(monkeypatch):
    assert web.choice() == ("duckduckgo", None)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    assert web.choice() == ("tavily", None)
    monkeypatch.setenv("TINYFISH_API_KEY", "tf-x")  # a key alone doesn't switch anything: WEB_SEARCH does
    assert web.choice() == ("tavily", None)


def test_choosing_a_service(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH", " TinyFish ")
    assert web.choice() == ("tinyfish", "WEB_SEARCH=tinyfish needs TINYFISH_API_KEY in .env.")
    monkeypatch.setenv("TINYFISH_API_KEY", "tf-x")
    assert web.choice() == ("tinyfish", None)
    assert "TinyFish" in web.describe()
    monkeypatch.setenv("WEB_SEARCH", "duckduckgo")
    assert web.choice() == ("duckduckgo", None)
    monkeypatch.setenv("WEB_SEARCH", "tinyfsh")
    name, problem = web.choice()
    assert "must be tinyfish, tavily or duckduckgo" in problem and "tinyfsh" in problem


def test_a_misconfigured_switch_is_an_error_not_a_silent_fallback(monkeypatch, fake):
    monkeypatch.setenv("WEB_SEARCH", "tinyfish")  # no key
    f = fake()
    out = web.web_search("anything")
    assert "isn't set up" in out["error"] and "TINYFISH_API_KEY" in out["error"]
    assert f.calls == []


# ---- TinyFish

def test_tinyfish_searches_then_reads_the_top_pages(monkeypatch, fake):
    monkeypatch.setenv("WEB_SEARCH", "tinyfish")
    monkeypatch.setenv("TINYFISH_API_KEY", "tf-secret")
    f = fake(fetch=fetched_pages)
    out = web.web_search("What does RuBisCO do in the Calvin cycle?")

    method, url, params, headers, _ = f.calls[0]
    assert (method, url) == ("GET", web.TINYFISH_SEARCH) and headers == {"X-API-Key": "tf-secret"}
    assert params["query"] == "What does RuBisCO do in the Calvin cycle?" and params["purpose"]
    method, url, _, headers, body = f.calls[1]
    assert (method, url) == ("POST", web.TINYFISH_FETCH) and headers == {"X-API-Key": "tf-secret"}
    assert body["urls"] == [r["url"] for r in SEARCH_JSON["results"][:3]]  # the top 3 by default
    assert body["format"] == "markdown" and body["per_url_timeout_ms"] == web.FETCH_PAGE_MS and body["purpose"]

    assert out["searched_with"] == "TinyFish" and len(out["results"]) == 4
    wiki, khan, brit, fourth = out["results"]
    assert wiki["site"] == "en.wikipedia.org" and brit["date"] == "2026-03-01"
    assert "RuBisCO to fix carbon dioxide" in wiki["content"]  # the paragraph that answers the question
    assert "Main menu" not in wiki["content"] and "upload.example" not in wiki["content"]  # nav, image, link target
    assert "Calvin cycle" in khan["content"]
    assert "content" not in brit and "content" not in fourth  # blocked page keeps its snippet; 4th wasn't fetched
    assert "content" in out["about_content"] and "note" not in out


def test_fetch_page_count_is_configurable(monkeypatch, fake):
    monkeypatch.setenv("WEB_SEARCH", "tinyfish")
    monkeypatch.setenv("TINYFISH_API_KEY", "tf-x")
    monkeypatch.setenv("TINYFISH_FETCH_PAGES", "0")
    f = fake()  # Fetch would raise
    out = web.web_search("rubisco")
    assert [c[0] for c in f.calls] == ["GET"] and out["searched_with"] == "TinyFish"
    assert all("content" not in r for r in out["results"])
    monkeypatch.setenv("TINYFISH_FETCH_PAGES", "1")
    f = fake(fetch=fetched_pages)
    web.web_search("rubisco")
    assert f.calls[1][4]["urls"] == [SEARCH_JSON["results"][0]["url"]]


def test_when_fetch_fails_the_snippets_still_answer(monkeypatch, fake):
    monkeypatch.setenv("WEB_SEARCH", "tinyfish")
    monkeypatch.setenv("TINYFISH_API_KEY", "tf-x")
    fake(fetch=(402, {"error": {"code": "INSUFFICIENT_CREDITS", "message": "..."}}))
    out = web.web_search("rubisco")
    assert out["searched_with"] == "TinyFish" and len(out["results"]) == 4
    assert all("content" not in r for r in out["results"])
    assert "search snippets only" in out["note"]


def test_when_tinyfish_search_fails_the_next_service_answers_and_says_so(monkeypatch, fake):
    monkeypatch.setenv("WEB_SEARCH", "tinyfish")
    monkeypatch.setenv("TINYFISH_API_KEY", "tf-bad")
    fake(search=(401, {"error": {"code": "INVALID_API_KEY", "message": "Invalid or expired API key"}}))
    monkeypatch.setattr(web, "_duckduckgo", lambda q, n: [{"title": "D", "url": "https://d.example/1", "snippet": "s"}])
    out = web.web_search("rubisco")
    assert out["searched_with"] == "DuckDuckGo" and out["results"][0]["site"] == "d.example"
    assert "didn't accept the API key" in out["note"] and "INVALID_API_KEY" in out["note"]
    assert "from DuckDuckGo" in out["note"]


def test_tinyfish_then_tavily_then_duckduckgo(monkeypatch, fake):
    monkeypatch.setenv("WEB_SEARCH", "tinyfish")
    monkeypatch.setenv("TINYFISH_API_KEY", "tf-x")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    fake(search=(429, {"error": {"code": "RATE_LIMIT_EXCEEDED", "message": "slow down"}}))
    monkeypatch.setattr(web, "_tavily", lambda q, n, k: [{"title": "T", "url": "https://t.example", "snippet": "s"}])
    out = web.web_search("rubisco")
    assert out["searched_with"] == "Tavily" and "too many requests" in out["note"]


def test_every_service_failing_is_reported(monkeypatch, fake):
    monkeypatch.setenv("WEB_SEARCH", "tinyfish")
    monkeypatch.setenv("TINYFISH_API_KEY", "tf-x")
    fake(search=(503, {"error": {"code": "SERVICE_BUSY", "message": "busy"}}))

    def ddg_down(q, n):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(web, "_duckduckgo", ddg_down)
    out = web.web_search("rubisco")
    assert out["error"].startswith("Web search failed") and "HTTP 503" in out["error"] and "DuckDuckGo" in out["error"]


def test_no_results_is_said_plainly(monkeypatch, fake):
    monkeypatch.setenv("WEB_SEARCH", "tinyfish")
    monkeypatch.setenv("TINYFISH_API_KEY", "tf-x")
    fake(search=(200, {"query": "q", "results": [], "total_results": 0, "page": 0}))
    out = web.web_search("zzqqxx")
    assert out == {"searched_with": "TinyFish", "results": [], "note": "No web results."}


# ---- the existing services are unchanged

def test_tavily_and_duckduckgo_still_work(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    monkeypatch.setattr(web, "_tavily", lambda q, n, k: [{"title": "T", "url": "https://www.t.example/a", "snippet": "x" * 900}])
    out = web.web_search("q")
    assert out["searched_with"] == "Tavily" and out["results"][0]["site"] == "t.example"
    assert len(out["results"][0]["snippet"]) == 600

    def tavily_down(q, n, k):
        raise httpx.ConnectTimeout("timeout")

    monkeypatch.setattr(web, "_tavily", tavily_down)
    monkeypatch.setattr(web, "_duckduckgo", lambda q, n: [{"title": "D", "url": "https://d.example", "snippet": "s"}])
    out = web.web_search("q")
    assert out["searched_with"] == "DuckDuckGo" and "Tavily failed" in out["note"]


# ---- excerpts

def test_excerpt_keeps_the_relevant_paragraphs_in_page_order():
    text = web.excerpt(PAGE, "What does RuBisCO do in the Calvin cycle?")
    assert "RuBisCO to fix carbon dioxide" in text
    assert "History" not in text and "Main menu" not in text
    assert "Photosynthesis is the process" not in text  # shares no words with the question


def test_excerpt_respects_the_limit_and_falls_back_to_the_start():
    long_page = "\n\n".join(f"Paragraph {i} about enzymes and the Calvin cycle in leaves, with detail." for i in range(80))
    text = web.excerpt(long_page, "calvin cycle enzymes", limit=500)
    assert len(text) <= 501 and text.startswith("Paragraph 0")
    start = web.excerpt(PAGE, "quantum chromodynamics")
    assert start.startswith("# Photosynthesis") and len(start) <= web.EXCERPT_CHARS + 1


def test_tool_reports_which_service_answered(monkeypatch):
    from tutor import tools

    monkeypatch.setattr(tools, "web_search", lambda q: {
        "searched_with": "TinyFish",
        "results": [{"title": "T", "site": "t.example", "url": "https://t.example", "snippet": "s", "content": "c"}]})
    t = tools.NotebookTools.__new__(tools.NotebookTools)
    outcome = t.web_search("q")
    assert outcome.ui == {"kind": "web", "query": "q", "via": "TinyFish",
                          "links": [{"title": "T", "url": "https://t.example", "site": "t.example"}]}
    assert outcome.result["results"][0]["content"] == "c"
    json.dumps(outcome.result)  # what goes to Gemini must be plain JSON
