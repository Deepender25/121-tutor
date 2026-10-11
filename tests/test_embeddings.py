"""Embedding modes: the online embedder (fake API client), and documents in two embedding spaces (fake embedders).
No network and no model downloads: the real EmbeddingGemma path is covered by test_server.py."""

import hashlib
import json
import time
from pathlib import Path

import numpy as np
import pytest
from google.genai import errors

from tutor import config
from tutor.retrieval.online import EmbeddingError, GeminiEmbedder, RateLimiter

SAMPLE = Path(__file__).parent / "fixtures" / "sample_toc.pdf"


# ---- the setting

@pytest.mark.parametrize("raw, mode, ok", [
    (None, "offline", True), ("", "offline", True), ("offline", "offline", True), ("online", "online", True),
    (" Online ", "online", True), ("gemini", "offline", False), ("local", "offline", False),
])
def test_embedding_mode_setting(raw, mode, ok):
    got, error = config.parse_embedding_mode(raw)
    assert got == mode
    assert (error is None) == ok
    if not ok:
        assert "'offline' or 'online'" in error


# ---- rate limiting

class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


def test_rate_limiter_keeps_headroom_for_questions_while_indexing():
    clock = FakeClock()
    limiter = RateLimiter(100, headroom=5, clock=clock, sleep=clock.sleep)
    assert limiter.acquire(95, background=True) == 0  # indexing may use 95 of 100 a minute
    assert limiter.acquire(5, background=False) == 0  # a live question still goes straight through
    waited = limiter.acquire(20, background=True)  # more indexing waits for the window to move on
    assert waited >= 60 - 1e-6
    limiter.block(30)
    t = clock.now
    limiter.acquire(1, background=False)
    assert clock.now - t >= 30  # after a 429 nobody sends until the API's delay has passed


# ---- the online embedder, against a fake Gemini client

def _vec(text: str, dim: int, salt: str = "") -> list[float]:
    seed = int(hashlib.sha256((salt + text).encode()).hexdigest()[:8], 16)
    return (np.random.default_rng(seed).normal(size=dim) * 3).tolist()  # not unit length: the class normalizes


class FakeModels:
    def __init__(self, dim, failures=()):
        self.dim = dim
        self.calls: list[list[str]] = []
        self.failures = list(failures)

    def embed_content(self, model, contents, config):
        if self.failures:
            raise self.failures.pop(0)
        texts = [c.parts[0].text for c in contents]
        assert all(len(c.parts) == 1 for c in contents)  # one Content per text, never a merged list of strings
        assert config.output_dimensionality == self.dim
        self.calls.append(texts)

        class R:
            embeddings = [type("E", (), {"values": _vec(t, self.dim)}) for t in texts]
        return R()


class FakeTokenizer:
    def encode(self, text, add_special_tokens=False):
        return type("Enc", (), {"ids": text.split()})


def online(failures=(), per_minute=100):
    clock = FakeClock()
    client = type("Client", (), {"models": FakeModels(768, failures)})()
    emb = GeminiEmbedder(client=client, tokenizer=FakeTokenizer(), per_minute=per_minute, sleep=clock.sleep)
    emb.limiter = RateLimiter(per_minute, clock=clock, sleep=clock.sleep)
    return emb, client.models, clock


def quota_error(per_day=False, delay="23s"):
    quota_id = "EmbedContentRequestsPerDayPerUserPerProjectPerModel-FreeTier" if per_day else \
        "EmbedContentRequestsPerMinutePerUserPerProjectPerModel-FreeTier"
    return errors.APIError(429, {"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED",
        "message": f"Quota exceeded for metric: embed_content_free_tier_requests, limit: 100\nPlease retry in {delay}.",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota_id}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay},
        ]}})


def test_online_embeds_each_text_separately_in_batches_with_retrieval_prompts():
    emb, models, _ = online()
    vectors = emb.embed_documents([(f"Chapter {i}", f"text {i}") for i in range(45)], batch_size=20)
    assert vectors.shape == (45, 768)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1)
    assert [len(c) for c in models.calls] == [20, 20, 5]
    assert models.calls[0][0] == "title: Chapter 0 | text: text 0"
    q = emb.embed_query("how big is the tank")
    assert q.shape == (768,) and models.calls[-1] == ["task: search result | query: how big is the tank"]
    assert emb.count_tokens("three little words") == 3


def test_online_never_sends_more_than_the_api_allows_per_request():
    emb, models, _ = online(per_minute=1000)
    emb.embed_documents([("t", str(i)) for i in range(250)], batch_size=500)
    assert max(len(c) for c in models.calls) == 100


def test_online_waits_out_the_per_minute_quota_as_the_api_asks():
    emb, models, clock = online(failures=[quota_error(delay="23s")])
    v = emb.embed_documents([("t", "a"), ("t", "b")])
    assert v.shape == (2, 768) and len(models.calls) == 1
    assert clock.now >= 23


def test_online_reports_a_used_up_daily_quota_without_waiting():
    emb, models, clock = online(failures=[quota_error(per_day=True)])
    with pytest.raises(EmbeddingError) as e:
        emb.embed_documents([("t", "a")])
    assert "daily" in str(e.value) and "EMBEDDING_MODEL=offline" in str(e.value)
    assert not e.value.transient and clock.now == 0


def test_online_explains_a_bad_key():
    bad = errors.APIError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                          "message": "API key not valid. Please pass a valid API key.",
                                          "details": [{"reason": "API_KEY_INVALID"}]}})
    emb, _, _ = online(failures=[bad])
    with pytest.raises(EmbeddingError) as e:
        emb.embed_query("x")
    assert "GEMINI_API_KEY" in str(e.value) and not e.value.transient


def test_online_retries_network_errors_then_reports_them_as_transient():
    emb, models, _ = online(failures=[ConnectionError("down")])
    assert emb.embed_query("x").shape == (768,)  # one blip: retried
    emb, models, _ = online(failures=[ConnectionError("down")] * 5)
    with pytest.raises(EmbeddingError) as e:
        emb.embed_query("x")
    assert e.value.transient and "internet" in str(e.value)


def test_online_rejects_a_response_with_the_wrong_number_of_vectors():
    emb, models, _ = online()
    original = models.embed_content

    def merged(model, contents, config):  # what a list of plain strings gets you: one vector for everything
        r = original(model, contents, config)
        r.embeddings = r.embeddings[:1]
        return r
    models.embed_content = merged
    with pytest.raises(EmbeddingError):
        emb.embed_documents([("t", "a"), ("t", "b")])


# ---- documents in two embedding spaces

class FakeEmbedder:
    """Deterministic vectors that differ per model: a stand-in for EmbeddingGemma vs Gemini Embedding 2."""

    def __init__(self, model_name, dim=16, fail_queries=False):
        self.model_name, self.dim, self.fail_queries = model_name, dim, fail_queries
        self.documents_embedded = 0
        self.queries = 0

    def count_tokens(self, text):
        return len(text.split())

    def _vecs(self, texts):
        v = np.array([_vec(t, self.dim, self.model_name) for t in texts], dtype=np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    def embed_documents(self, items, show_progress=False, on_progress=None, batch_size=16):
        self.documents_embedded += len(items)
        if on_progress:
            on_progress(len(items), len(items))
        return self._vecs([f"{t} {x}" for t, x in items])

    def embed_query(self, query):
        self.queries += 1
        if self.fail_queries:
            raise EmbeddingError("Couldn't reach the Gemini embedding service.", transient=True)
        return self._vecs([query])[0]


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    from tutor import library as lib
    return lib


def _ingest(lib, embedder):
    return lib.ingest(SAMPLE, embedder, log=lambda *a: None, remember_current=False)


def _meta(doc_id):
    return json.loads((config.doc_dir(doc_id) / "meta.json").read_text(encoding="utf-8"))


def test_switching_models_adds_a_space_from_stored_chunks_and_keeps_progress(library):
    lib = library
    offline, online_ = FakeEmbedder("google/embeddinggemma-2"), FakeEmbedder("gemini-embedding-2")
    first = _ingest(lib, offline)
    doc_id = first.doc_id
    assert not first.skipped and list(lib.vector_indexes(_meta(doc_id))) == ["google/embeddinggemma-2@16/p1"]

    lesson = lib.open_document(doc_id, offline).lesson
    lesson.read("next")
    lesson.mark("1", completed=True)
    progress_before = json.loads((config.doc_dir(doc_id) / "progress.json").read_text(encoding="utf-8"))

    # Switched to online: the document opens, isn't in the new space yet, and is queued for it.
    assert lib.missing_embeddings(online_) == [doc_id]
    doc = lib.open_document(doc_id, online_)
    assert doc.needs_embedding and doc.store is None
    assert [c.text for c in doc.lesson.chunks] == [c.text for c in lesson.chunks]

    assert lib.add_embeddings(doc_id, online_, log=lambda *a: None) is True
    assert online_.documents_embedded == len(lesson.chunks)  # embedded from stored chunks, not re-parsed
    meta = _meta(doc_id)
    assert set(lib.vector_indexes(meta)) == {"google/embeddinggemma-2@16/p1", "gemini-embedding-2@16/p1"}
    tables = {e["table"] for e in meta["vector_indexes"].values()}
    assert len(tables) == 2  # never one table for two models
    assert json.loads((config.doc_dir(doc_id) / "progress.json").read_text(encoding="utf-8")) == progress_before
    assert lib.missing_embeddings(online_) == [] and lib.add_embeddings(doc_id, online_) is False

    # Both modes now open it with their own vectors; switching back costs nothing.
    assert not lib.open_document(doc_id, online_).needs_embedding
    assert not lib.open_document(doc_id, offline).needs_embedding
    again = _ingest(lib, offline)
    assert again.skipped and offline.documents_embedded == len(lesson.chunks)


def test_uploading_a_known_pdf_in_the_other_mode_only_adds_vectors(library):
    lib = library
    offline, online_ = FakeEmbedder("google/embeddinggemma-2"), FakeEmbedder("gemini-embedding-2")
    doc_id = _ingest(lib, offline).doc_id
    result = _ingest(lib, online_)
    assert result.reembedded and not result.skipped and result.doc_id == doc_id
    assert "gemini-embedding-2@16/p1" in lib.vector_indexes(_meta(doc_id))


def test_search_never_mixes_spaces_and_falls_back_to_keywords(library):
    lib = library
    from tutor.tools import NotebookTools

    offline, online_ = FakeEmbedder("google/embeddinggemma-2"), FakeEmbedder("gemini-embedding-2")
    doc_id = _ingest(lib, offline).doc_id

    # Not yet re-embedded: keyword results only, the question isn't embedded at all, and the tutor is told.
    tools = NotebookTools(lib.DocumentCache(online_), online_, [doc_id], doc_id)
    out = tools.search_sources("first flush diverter")
    assert out.result["results"] and online_.queries == 0
    assert "keyword" in out.result["note"]

    # Re-embedded: the question is embedded with the online model and searched in the online table only.
    lib.add_embeddings(doc_id, online_, log=lambda *a: None)
    tools = NotebookTools(lib.DocumentCache(online_), online_, [doc_id], doc_id)
    out = tools.search_sources("first flush diverter")
    assert out.result["results"] and online_.queries == 1 and "note" not in out.result

    # The service is unreachable: still answers from keywords, and says so.
    down = FakeEmbedder("gemini-embedding-2", fail_queries=True)
    tools = NotebookTools(lib.DocumentCache(down), down, [doc_id], doc_id)
    out = tools.search_sources("first flush diverter")
    assert out.result["results"] and "keyword matches only" in out.result["note"]


def test_documents_indexed_before_spaces_existed_still_open(library):
    lib = library
    from tutor.retrieval.store import TABLE, ChunkStore

    offline = FakeEmbedder("google/embeddinggemma-2")
    doc_id = _ingest(lib, offline).doc_id
    folder = config.doc_dir(doc_id)
    meta = _meta(doc_id)
    # Rebuild the old layout: one "chunks" table, the model in meta, no vector_indexes.
    current = ChunkStore(folder / "lancedb", meta["vector_indexes"]["google/embeddinggemma-2@16/p1"]["table"])
    chunks = current.load_chunks()
    vectors = np.array(current.table.to_arrow()["vector"].to_pylist(), dtype=np.float32)
    ChunkStore(folder / "lancedb", TABLE).write(doc_id, chunks, vectors)
    legacy = {k: v for k, v in meta.items() if k != "vector_indexes"}
    legacy.update(embedding_model="google/embeddinggemma-2", embedding_dim=16, embedding_prompt_version=1)
    (folder / "meta.json").write_text(json.dumps(legacy), encoding="utf-8")

    doc = lib.open_document(doc_id, offline)
    assert doc.store is not None and doc.store.table_name == TABLE
    online_ = FakeEmbedder("gemini-embedding-2")
    lib.add_embeddings(doc_id, online_, log=lambda *a: None)
    meta = _meta(doc_id)
    assert "embedding_model" not in meta  # rewritten in the new format, the old table still listed
    assert meta["vector_indexes"]["google/embeddinggemma-2@16/p1"]["table"] == TABLE


def test_the_server_re_embeds_in_the_background_once_per_document(library):
    lib = library
    from tutor.server.state import AppState

    offline, online_ = FakeEmbedder("google/embeddinggemma-2"), FakeEmbedder("gemini-embedding-2")
    doc_id = _ingest(lib, offline).doc_id
    state = AppState()
    state.embedder, state.cache = online_, lib.DocumentCache(online_)
    state.reindex_missing()
    state.ensure_embeddings(doc_id)  # already queued: no second job
    jobs = state.jobs.for_notebook("some-notebook", [doc_id], include_finished=True)
    assert len(jobs) == 1 and jobs[0].kind == "reindex"
    deadline = time.monotonic() + 30
    while jobs[0].status in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert jobs[0].status == "done", jobs[0].error
    assert not state.cache.get(doc_id).needs_embedding
    state.jobs.shutdown()
