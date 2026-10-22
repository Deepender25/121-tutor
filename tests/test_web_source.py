"""Adding a source from a link: the public-address check, PDF downloads, and web pages read with TinyFish Fetch
and laid out as a PDF whose outline is the page's headings. TinyFish and the network are faked."""

import contextlib
import unicodedata

import pymupdf
import pytest

from tutor import web, web_source
from tutor.ingest.parse import parse_pdf
from tutor.web_source import LinkError

PAGE_HTML = """
<nav><a href="/">Home</a> · <a href="/donate">Donate</a></nav>
<script>alert('x')</script><style>body{color:red}</style>
<h1>Photosynthesis</h1>
<p class="lead" style="color:blue" onclick="evil()">Plants turn light into chemical energy. Note: the class="x" here is text.</p>
<img src="https://img.example/leaf.png" alt="leaf">
<h2>Light reactions</h2>
<p>Water is split in the thylakoid membranes, releasing oxygen.</p>
<h3>Photosystem II</h3>
<p>Photosystem II absorbs light at 680 nm.</p>
<h2>Calvin cycle</h2>
<p>RuBisCO fixes carbon dioxide in the stroma.</p>
<form><p>Subscribe to our newsletter for study tips.</p><input type="email"></form>
"""


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("WEB_SEARCH", "TINYFISH_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.delenv(k, raising=False)


# ---- public addresses only

@pytest.mark.parametrize("url", [
    "ftp://example.com/a.pdf", "example.com/a.pdf", "", "http://localhost:8766/api/status", "http://127.0.0.1/x",
    "http://10.0.0.5/x", "http://192.168.1.1/x", "http://169.254.169.254/latest/meta-data", "http://[::1]/x",
])
def test_private_or_malformed_addresses_are_refused(url):
    with pytest.raises(LinkError):
        web_source.check_public(url)


def test_a_public_address_is_accepted():
    assert web_source.check_public("  https://93.184.216.34/notes.pdf ") == "https://93.184.216.34/notes.pdf"


# ---- downloading a PDF

class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"", location=None):
        self.status_code = status
        self.headers = headers or {}
        if location:
            self.headers["location"] = location
        self.is_redirect = status in (301, 302, 303, 307, 308)
        self._body = body

    def iter_bytes(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]


@pytest.fixture
def net(monkeypatch):
    """httpx.stream faked by URL; check_public lets *.example through and records every URL it was asked about."""
    routes, checked = {}, []

    def check(url):
        checked.append(url)
        if "169.254" in url:
            raise LinkError("private")
        return url

    @contextlib.contextmanager
    def stream(method, url, **kw):
        assert kw.get("follow_redirects") is False  # every hop must come back through check_public
        yield routes[url]

    monkeypatch.setattr(web_source, "check_public", check)
    monkeypatch.setattr(web_source.httpx, "stream", stream)
    return routes, checked


def test_a_pdf_link_is_downloaded(net):
    routes, _ = net
    routes["https://a.example/notes.pdf"] = FakeResponse(headers={"content-type": "application/pdf"},
                                                         body=b"%PDF-1.7\n" + b"x" * 200_000)
    data = web_source.download_pdf("https://a.example/notes.pdf", 10 * 1024 * 1024)
    assert data.startswith(b"%PDF-") and len(data) == 200_009


def test_a_web_page_is_not_a_pdf(net):
    routes, _ = net
    routes["https://a.example/page"] = FakeResponse(headers={"content-type": "text/html"}, body=b"<html>hi</html>")
    assert web_source.download_pdf("https://a.example/page", 1024) is None


def test_redirects_are_checked_at_every_hop(net):
    routes, checked = net
    routes["https://a.example/go"] = FakeResponse(302, location="http://169.254.169.254/latest")
    with pytest.raises(LinkError):
        web_source.download_pdf("https://a.example/go", 1024)
    assert checked == ["https://a.example/go", "http://169.254.169.254/latest"]


def test_size_limit_and_fake_pdfs(net):
    routes, _ = net
    routes["https://a.example/big.pdf"] = FakeResponse(body=b"%PDF-" + b"x" * 5000)
    with pytest.raises(LinkError, match="larger than"):
        web_source.download_pdf("https://a.example/big.pdf", 1024)
    routes["https://a.example/fake.pdf"] = FakeResponse(headers={"content-type": "application/pdf"}, body=b"<html>")
    with pytest.raises(LinkError, match="isn't a valid PDF"):
        web_source.download_pdf("https://a.example/fake.pdf", 1024)
    routes["https://a.example/gone.pdf"] = FakeResponse(404)
    with pytest.raises(LinkError, match="HTTP 404"):
        web_source.download_pdf("https://a.example/gone.pdf", 1024)
    routes["https://a.example/wiki/Page"] = FakeResponse(403)  # refuses this request; TinyFish's browser may not
    assert web_source.download_pdf("https://a.example/wiki/Page", 1024) is None


# ---- a web page becomes a PDF source

def test_html_becomes_a_pdf_whose_outline_is_the_headings(tmp_path):
    data = web_source.html_to_pdf(PAGE_HTML, "Photosynthesis - Wikipedia", "https://en.wikipedia.org/wiki/Photosynthesis")
    path = tmp_path / "page.pdf"
    path.write_bytes(data)
    doc = pymupdf.open(path)
    assert doc.get_toc() == [[1, "Photosynthesis", 1], [2, "Light reactions", 1], [3, "Photosystem II", 1],
                             [2, "Calvin cycle", 1]]
    # NFKC: the layout engine writes "fi" as the ligature "ﬁ"; the PDF reader and citation highlights expand it
    text = " ".join(unicodedata.normalize("NFKC", " ".join(page.get_text() for page in doc)).split())
    assert "Source: https://en.wikipedia.org/wiki/Photosynthesis" in text
    assert "RuBisCO fixes carbon dioxide" in text and "Subscribe to our newsletter" in text  # forms keep their text
    assert 'the class="x" here is text' in text  # attributes are cleaned inside tags only
    assert "alert" not in text and "Donate" not in text and "color:red" not in text
    assert doc.metadata["title"] == "Photosynthesis - Wikipedia"
    doc.close()
    parsed = parse_pdf(path)  # the normal PDF pipeline reads it with the page's own structure
    assert parsed.structure_source == "outline"
    body = " ".join(p.text for sec in parsed.sections for p in sec.paragraphs)
    assert "RuBisCO fixes carbon dioxide" in body  # no ligatures in what gets indexed and searched
    assert [s.title for s in parsed.sections][:4] == ["Photosynthesis", "Light reactions", "Photosystem II", "Calvin cycle"]


def test_a_page_without_an_h1_gets_the_title_as_its_heading():
    data = web_source.html_to_pdf("<p>Just a paragraph of notes about enzymes.</p>", "Enzymes", "https://e.example")
    doc = pymupdf.open("pdf", data)
    assert doc.get_toc()[0][1] == "Enzymes"


def test_web_pages_need_tinyfish(monkeypatch):
    with pytest.raises(LinkError, match="WEB_SEARCH=tinyfish"):
        web_source.page_to_pdf("https://a.example/page")
    monkeypatch.setenv("WEB_SEARCH", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    with pytest.raises(LinkError, match="WEB_SEARCH=tinyfish"):
        web_source.page_to_pdf("https://a.example/page")


def test_a_page_read_with_tinyfish_fetch(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH", "tinyfish")
    monkeypatch.setenv("TINYFISH_API_KEY", "tf-x")
    sent = {}

    def fetch(urls, key, fmt="markdown", purpose="", per_page_ms=0):
        sent.update(urls=urls, key=key, fmt=fmt, purpose=purpose, per_page_ms=per_page_ms)
        return [{"url": urls[0], "final_url": "https://en.wikipedia.org/wiki/Photosynthesis", "title": "Photosynthesis",
                 "format": "html", "text": PAGE_HTML}], []

    monkeypatch.setattr(web, "tinyfish_fetch", fetch)
    data, title = web_source.page_to_pdf("https://en.wikipedia.org/wiki/Photosynthesis")
    assert sent["fmt"] == "html" and sent["key"] == "tf-x" and sent["purpose"] == web.SOURCE_PURPOSE
    assert title == "Photosynthesis" and data.startswith(b"%PDF-")


@pytest.mark.parametrize("code,message", [
    ("bot_blocked", "blocked automated reading"), ("login_required", "needs a login"),
    ("page_not_found", "doesn't exist"), ("something_new", "something_new"),
])
def test_tinyfish_fetch_errors_are_explained(monkeypatch, code, message):
    monkeypatch.setenv("WEB_SEARCH", "tinyfish")
    monkeypatch.setenv("TINYFISH_API_KEY", "tf-x")
    monkeypatch.setattr(web, "tinyfish_fetch", lambda *a, **k: ([], [{"url": "u", "error": code}]))
    with pytest.raises(LinkError, match=message):
        web_source.page_to_pdf("https://a.example/page")


def test_tinyfish_account_problems_are_explained(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH", "tinyfish")
    monkeypatch.setenv("TINYFISH_API_KEY", "tf-x")

    def broke(*a, **k):
        raise web.WebError("TinyFish Fetch failed: today's free allowance is used up and the TinyFish wallet is empty")

    monkeypatch.setattr(web, "tinyfish_fetch", broke)
    with pytest.raises(LinkError, match="free allowance"):
        web_source.page_to_pdf("https://a.example/page")
