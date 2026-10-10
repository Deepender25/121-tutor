from pathlib import Path

import pytest

from tutor.ingest.parse import parse_pdf

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED = [
    (1, "1 Introduction"), (2, "1.1 Why harvest rainwater"), (2, "1.2 Legal status"),
    (1, "2 System Components"), (2, "2.1 Catchment"), (2, "2.2 First-flush diverter"), (2, "2.3 Filters"),
    (1, "3 Design"), (2, "3.1 Storage tank sizing"), (2, "3.2 Maintenance schedule"), (1, "4 Conclusion"),
]


@pytest.fixture(scope="module", params=["sample_toc", "sample_notoc"])
def parsed(request):
    path = FIXTURES / f"{request.param}.pdf"
    if not path.exists():
        pytest.skip("run tests/fixtures/make_sample_pdf.py first")
    return request.param, parse_pdf(path)


def test_structure(parsed):
    name, doc = parsed
    assert doc.structure_source == ("outline" if name == "sample_toc" else "headings")
    assert [(s.level, s.title) for s in doc.sections] == EXPECTED
    assert doc.title == "Rainwater Harvesting Guide"


def test_pages_and_paths(parsed):
    _, doc = parsed
    by_title = {s.title: s for s in doc.sections}
    assert by_title["1.2 Legal status"].page_start == 2
    assert by_title["3.1 Storage tank sizing"].heading_path == "3 Design › 3.1 Storage tank sizing"
    assert (by_title["2 System Components"].page_start, by_title["2 System Components"].page_end) == (3, 4)
    assert by_title["1.2 Legal status"].paragraphs[0].text.startswith("Under the Water Act 2019")


def test_running_header_footer_removed(parsed):
    _, doc = parsed
    texts = [p.text for s in doc.sections for p in s.paragraphs]
    assert not any("Draft 2" in t for t in texts)
    assert not any(t.strip().startswith("Page ") for t in texts)
    assert not any(t.strip() == s.title for s in doc.sections for t in texts)  # headings not duplicated in body


def test_strip_tags_from_headings():
    from tutor.document import strip_tags

    assert strip_tags("<mark>PART A — NATURAL LANGUAGE</mark>") == "PART A — NATURAL LANGUAGE"
    assert strip_tags("a < b and c > d") == "a < b and c > d"  # comparisons aren't tags
