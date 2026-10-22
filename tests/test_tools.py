"""NotebookTools over a small real LanceDB table, with a source that can't be opened next to a healthy one."""

from types import SimpleNamespace

import numpy as np
import pytest

from tutor.document import Chunk, Section
from tutor.lesson import Lesson
from tutor.library import DocumentError, StaleIndexError
from tutor.retrieval.store import ChunkStore
from tutor.tools import NotebookTools

DIM = 8
OVERLAP = "the end of the previous passage about gutters. "
BODIES = [
    "Gutters carry rain from the roof to the first flush diverter on page one.",
    "The mesh filter size should be 200 micron so that leaves and grit stay out of the tank.",
    "Chlorine dosing keeps the stored water safe: two milligrams per litre is enough.",
]


class FakeEmbedder:
    def embed_query(self, text: str) -> np.ndarray:
        return np.ones(DIM, dtype=np.float32) / np.sqrt(DIM)


class FakeCache:
    """DocumentCache stand-in: known ids open, anything else fails like a missing or stale index."""

    def __init__(self, docs: dict):
        self.docs = docs

    def get(self, doc_id: str):
        if doc_id == "stale":
            raise StaleIndexError("Index was built with different settings.", source_file=None)
        if doc_id not in self.docs:
            raise DocumentError(f"Document {doc_id} isn't ingested.")
        return self.docs[doc_id]


@pytest.fixture
def healthy(tmp_path):
    sections = [Section("s000", 1, "1 Rain", "1 Rain", 1, 3, first_order=0, last_order=2)]
    chunks = [Chunk(i, "s000", "1 Rain", i + 1, i + 1, (OVERLAP if i else "") + body,
                    overlap_chars=len(OVERLAP) if i else 0) for i, body in enumerate(BODIES)]
    store = ChunkStore(tmp_path / "lancedb")
    vectors = np.random.default_rng(0).normal(size=(len(chunks), DIM)).astype(np.float32)
    store.write("good", chunks, vectors)
    lesson = Lesson("Rain Guide", sections, store.load_chunks(), None)
    return SimpleNamespace(doc_id="good", meta={"title": "Rain Guide"}, store=store, lesson=lesson, text_store=store)


def tools_for(doc, *doc_ids):
    return NotebookTools(FakeCache({"good": doc}), FakeEmbedder(), list(doc_ids), "good")


def test_outline_lists_a_broken_source_as_unavailable(healthy):
    tools = tools_for(healthy, "good", "missing", "stale")
    out = tools.call("get_outline", {})
    assert "error" not in out.result
    assert [s["title"] for s in out.result["sources"]] == ["Rain Guide", "(unavailable source)", "(unavailable source)"]
    assert [s.get("unavailable", False) for s in out.result["sources"]] == [False, True, True]
    assert out.result["outline"]["title"] == "Rain Guide"
    assert "error" in tools.call("get_outline", {"source": "2"}).result  # asking for the broken one is a tool error


def test_search_skips_a_broken_source_and_says_so(healthy):
    out = tools_for(healthy, "good", "missing").call("search_sources", {"query": "mesh filter size micron"})
    assert "error" not in out.result
    assert "Couldn't open: source 2" in out.result["note"]
    assert all(r["source"] == "Rain Guide" for r in out.result["results"])


def test_search_with_no_source_that_opens_is_a_speakable_error(healthy):
    out = tools_for(healthy, "missing", "stale").call("search_sources", {"query": "mesh filter"})
    assert "None of this notebook's sources can be opened" in out.result["error"]


def test_search_results_drop_the_overlap_from_the_previous_chunk(healthy):
    out = tools_for(healthy, "good").call("search_sources", {"query": "mesh filter size micron"})
    first = out.result["results"][0]
    assert "200 micron" in first["text"]
    assert first["text"] == BODIES[1]  # the chunk body: no text copied from the passage before it
    by_page = {r["pages"]: r["text"] for r in out.result["results"]}
    assert all(not text.startswith(OVERLAP) for text in by_page.values())
    assert not any(ref["snippet"].startswith("the end of the previous") for ref in out.ui["refs"])


def test_open_source_switches_the_active_source_and_shows_a_page(healthy):
    switched = []
    tools = NotebookTools(FakeCache({"good": healthy, "other": healthy}), FakeEmbedder(), ["other", "good"], "other",
                          switched.append)
    out = tools.call("open_source", {"source": "2", "page": 3})
    assert out.result["opened"] == "Rain Guide" and out.result["showing_page"] == 3 and out.result["pages"] == 3
    assert out.result["sections"] and out.result["reading_position"] == {"started": False}
    assert out.ui == {"kind": "open", "doc_id": "good", "page": 3}
    assert tools.active == "good" and switched == ["good"]
    # no page: where reading left off, else page 1; the reading position doesn't move
    healthy.lesson.read("page:2")
    assert tools.call("open_source", {"source": "2"}).ui["page"] == 2
    assert healthy.lesson.position()["pages"] == "2"


def test_open_source_rejects_a_page_that_isnt_there(healthy):
    tools = tools_for(healthy, "good")
    out = tools.call("open_source", {"page": 99})
    assert "pages 1 to 3" in out.result["error"] and out.ui["kind"] == "error"


def test_each_passage_says_to_teach_it_before_reading_on(healthy):
    tools = tools_for(healthy, "good")
    first = tools.call("read_passage", {"target": "start"}).result
    assert first["next_step"].startswith("Teach this part now") and "target next" in first["next_step"]
    last = tools.call("read_passage", {"target": "page:3"}).result
    assert last["is_section_end"] and "explain it back" in last["next_step"]
