"""API integration tests: real embedder + LanceDB in a temp data folder (see conftest.py).
Gemini is not called here (quota); the live socket is tested only for its failure paths."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tutor import config
from tutor.notebooks import COVERS as NOTEBOOK_COVERS

SAMPLE = Path(__file__).parent / "fixtures" / "sample_toc.pdf"


@pytest.fixture(scope="module")
def client():
    if not SAMPLE.exists():
        pytest.skip("run tests/fixtures/make_sample_pdf.py first")
    from tutor.server.app import app, state

    with TestClient(app) as c:
        assert state.embedder_ready.wait(300), "embedder did not load"
        assert state.embedder is not None, state.embedder_error
        yield c


def upload_and_wait(client, notebook_id: str, path: Path = SAMPLE) -> dict:
    with path.open("rb") as f:
        r = client.post(f"/api/notebooks/{notebook_id}/sources", files={"files": (path.name, f, "application/pdf")})
    assert r.status_code == 202, r.text
    job_id = r.json()["jobs"][0]["id"]
    deadline = time.time() + 300
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            assert job["status"] == "done", job
            return job
        time.sleep(0.3)
    raise AssertionError("ingest timed out")


@pytest.fixture(scope="module")
def notebook(client):
    nb = client.post("/api/notebooks", json={"name": "Rain"}).json()
    job = upload_and_wait(client, nb["id"])
    return {**nb, "doc_id": job["doc_id"]}


def test_health_and_status(client):
    assert client.get("/api/health").json() == {"ok": True, "app": "voice-tutor"}
    status = client.get("/api/status").json()
    assert status["embedder"] == "ready"
    assert status["voice"] in status["voices"]


def test_root_points_to_the_ui(client):
    """The API server doesn't serve pages: opening it directly sends you to the UI server."""
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == f"{config.UI_URL}/"


def test_notebook_has_indexed_source(client, notebook):
    data = client.get(f"/api/notebooks/{notebook['id']}").json()
    assert data["active_source"] == notebook["doc_id"]
    [source] = data["sources"]
    assert source["title"] == "Rainwater Harvesting Guide"
    assert source["pages"] == 6 and source["chunks"] == 11
    assert (config.FILES_DIR / f"{notebook['doc_id']}.pdf").exists()


def test_outline_pages_and_highlight(client, notebook):
    doc = notebook["doc_id"]
    outline = client.get(f"/api/docs/{doc}/outline").json()
    assert [s["title"] for s in outline["sections"]][:3] == ["1 Introduction", "1.1 Why harvest rainwater", "1.2 Legal status"]
    png = client.get(f"/api/docs/{doc}/page/5.png?scale=1.5")
    assert png.status_code == 200 and png.headers["content-type"] == "image/png" and png.content[:4] == b"\x89PNG"
    assert client.get(f"/api/docs/{doc}/page/99.png").status_code == 404
    rects = client.get(f"/api/docs/{doc}/page/5/find", params={"text": "Tank capacity in litres equals roof area in square metres times annual rainfall"}).json()["rects"]
    assert rects and all(0 <= v <= 1 for r in rects for v in r)


def test_page_sizes(client, notebook):
    sizes = client.get(f"/api/docs/{notebook['doc_id']}/pages").json()["sizes"]
    assert len(sizes) == 6 and all(w > 0 and h > 0 for w, h in sizes)
    assert client.get(f"/api/docs/{notebook['doc_id']}/pages").json()["sizes"] == sizes  # served from the cache


def test_locate_sentence_for_citation_highlights(client, notebook):
    doc = notebook["doc_id"]
    # markdown and a different case don't matter; the page range is a hint (the neighbour is searched too)
    hit = client.get(f"/api/docs/{doc}/locate", params={
        "text": "**Tank capacity** in litres equals roof area in square metres times annual rainfall", "first": 4, "last": 4}).json()
    assert hit["page"] == 5 and hit["rects"] and all(0 <= v <= 1 for r in hit["rects"] for v in r)
    miss = client.get(f"/api/docs/{doc}/locate", params={"text": "quantum chromodynamics of gluon plasma", "first": 1, "last": 6}).json()
    assert miss == {"page": None, "rects": []}


def test_search_endpoint(client, notebook):
    refs = client.get(f"/api/notebooks/{notebook['id']}/search", params={"q": "how much chlorine for the tank"}).json()["refs"]
    assert refs[0]["page"] == 6 and "chlorine" in refs[0]["snippet"]


def test_rename_and_icon(client, notebook):
    r = client.patch(f"/api/notebooks/{notebook['id']}", json={"name": "  Rainwater  ", "icon": "leaf"}).json()
    assert r["name"] == "Rainwater" and r["icon"] == "leaf"
    assert client.patch(f"/api/notebooks/{notebook['id']}", json={"icon": "🌧️"}).status_code == 400
    assert client.patch(f"/api/notebooks/{notebook['id']}", json={"active_source": "nope"}).status_code == 400
    # book cover colour: one of the palette, changeable, anything else refused
    assert client.get(f"/api/notebooks/{notebook['id']}").json()["cover"] in NOTEBOOK_COVERS
    assert client.patch(f"/api/notebooks/{notebook['id']}", json={"cover": "teal"}).json()["cover"] == "teal"
    assert client.patch(f"/api/notebooks/{notebook['id']}", json={"cover": "neon"}).status_code == 400


def test_tools_and_prompt(client, notebook):
    from tutor.server.app import state
    from tutor.tools import NotebookTools
    from tutor.voice.prompt import build_system_prompt

    nb = state.notebooks.get(notebook["id"])
    tools = NotebookTools(state.cache, state.embedder, nb.sources, nb.active_source)
    passage = tools.call("read_passage", {"target": "chapter 3"})
    assert passage.result["section"] == "3 Design" and passage.ui["page"] == 5
    assert "text" not in passage.ui  # the browser gets the citation (page, snippet), not the passage
    found = tools.call("search_sources", {"query": "mesh filter size"})
    assert "200 micron" in found.result["results"][0]["text"]
    assert all("text" not in r for r in found.ui["refs"])
    assert "error" in tools.call("read_passage", {"source": "9"}).result
    prompt = build_system_prompt(state.notebooks.get(notebook["id"]), state.cache)
    assert "Rainwater Harvesting Guide" in prompt and "3.1 Storage tank sizing" in prompt
    assert "{" not in prompt.split("# Ground rules")[0]  # every placeholder filled


def test_completion_tracking_is_gone(client, notebook):
    """No percentages, ticks or marking anywhere: not for the tutor, not in the API, not in the prompt."""
    from tutor.server.app import state
    from tutor.tools import DECLARATIONS, NotebookTools
    from tutor.voice.prompt import build_system_prompt

    doc = notebook["doc_id"]
    assert "update_progress" not in {d["name"] for d in DECLARATIONS}
    nb = state.notebooks.get(notebook["id"])
    tools = NotebookTools(state.cache, state.embedder, nb.sources, nb.active_source)
    assert "Unknown tool" in tools.call("update_progress", {"target": "all"}).result["error"]
    for path in (f"/api/docs/{doc}/progress", f"/api/docs/{doc}/reset"):
        assert client.post(path, json={"target": "all"}).status_code in (404, 405), path
    outline = client.get(f"/api/docs/{doc}/outline").json()
    assert "document_covered" not in outline and all(set(s) == {"id", "title", "level", "pages"} for s in outline["sections"])
    source = client.get(f"/api/notebooks/{notebook['id']}").json()["sources"][0]
    assert "covered" not in source
    passage = tools.call("read_passage", {"target": "start"})
    assert "document_covered" not in passage.result and "covered" not in passage.ui
    prompt = build_system_prompt(nb, state.cache)
    assert "covered" not in prompt.split("# Reading position")[0].split("sources")[-1] and "(completed)" not in prompt


def test_shared_source_lifecycle(client, notebook):
    other = client.post("/api/notebooks", json={"name": "Other"}).json()
    job = upload_and_wait(client, other["id"])  # same file: reuses the index
    assert job["doc_id"] == notebook["doc_id"]
    # Removing it from one notebook keeps the index (still used by the first one).
    assert client.delete(f"/api/notebooks/{other['id']}/sources/{job['doc_id']}").status_code == 204
    assert (config.doc_dir(job["doc_id"]) / "meta.json").exists()
    assert client.delete(f"/api/notebooks/{other['id']}").status_code == 204
    assert client.get(f"/api/notebooks/{other['id']}").status_code == 404
    assert (config.doc_dir(job["doc_id"]) / "meta.json").exists()


def test_rejects_non_pdf(client, notebook):
    r = client.post(f"/api/notebooks/{notebook['id']}/sources", files={"files": ("notes.pdf", b"hello", "application/pdf")})
    assert r.status_code == 415


def test_live_socket_unknown_notebook(client):
    with client.websocket_connect("/api/notebooks/doesnotexist/live") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["fatal"]


def test_delete_last_user_removes_index(client):
    nb = client.post("/api/notebooks", json={"name": "Temp"}).json()
    fixture = Path(__file__).parent / "fixtures" / "sample_notoc.pdf"
    job = upload_and_wait(client, nb["id"], fixture)
    assert (config.doc_dir(job["doc_id"]) / "meta.json").exists()
    client.delete(f"/api/notebooks/{nb['id']}")
    assert not config.doc_dir(job["doc_id"]).exists()
    assert not (config.FILES_DIR / f"{job['doc_id']}.pdf").exists()


def test_settings_save_does_not_hang(client):
    """Regression: saving a voice used to deadlock (non-reentrant lock taken twice), after which every
    /api/status call hung and Settings could not be opened."""
    import concurrent.futures

    def roundtrip():
        saved = client.put("/api/settings", json={"voice": "Puck"}).json()
        status = client.get("/api/status").json()
        again = client.put("/api/settings", json={"voice": "Kore"}).json()
        return saved, status, again, client.get("/api/status").json()

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        saved, status, again, final = pool.submit(roundtrip).result(timeout=10)
    assert saved["voice"] == "Puck" and status["voice"] == "Puck"
    assert again["voice"] == "Kore" and final["voice"] == "Kore"
    assert client.put("/api/settings", json={"voice": "Nobody"}).status_code == 400


def test_tutor_manner_setting(client, notebook):
    """Encouragement, expression and teaching style: saved, validated, and written into the tutor's prompt."""
    from tutor.server.app import state
    from tutor.voice.prompt import build_system_prompt

    status = client.get("/api/status").json()
    assert status["style"] == {"warmth": "balanced", "expression": "natural", "teaching": "direct"}
    assert [o["value"] for o in status["style_options"]["warmth"]["options"]] == ["minimal", "balanced", "warm"]
    saved = client.put("/api/settings", json={"style": {"warmth": "minimal", "teaching": "exam"}}).json()
    assert saved["style"] == {"warmth": "minimal", "expression": "natural", "teaching": "exam"}
    assert saved["voice"] == client.get("/api/status").json()["voice"]  # untouched
    assert client.put("/api/settings", json={"style": {"warmth": "gushing"}}).status_code == 400
    assert client.put("/api/settings", json={"style": {"mood": "calm"}}).status_code == 400

    prompt = build_system_prompt(state.notebooks.get(notebook["id"]), state.cache, state.settings()["style"])
    assert "# Your manner" in prompt and "No praise" in prompt and "Teach for exams" in prompt
    assert "Be warm and encouraging" not in prompt and "{style_block}" not in prompt
    client.put("/api/settings", json={"style": {"warmth": "balanced", "teaching": "direct"}})


def test_legacy_emoji_notebooks_get_an_icon():
    from tutor.notebooks import Notebook

    assert Notebook("abc", "Old", "🌱", "t", "t").icon == "leaf"
    assert Notebook("abc", "Old", "🦄", "t", "t").icon == "book"
    assert Notebook("abc", "New", "", "t", "t", icon="brain").icon == "brain"


def test_voice_preview_is_generated_once_and_cached(client, monkeypatch):
    from tutor.voice import preview

    calls = []

    async def fake_synthesize(voice):
        calls.append(voice)
        return b"\x00\x01" * 2400  # 0.1 s of 24 kHz PCM

    monkeypatch.setattr(preview, "synthesize", fake_synthesize)
    first = client.get("/api/voices/Leda/preview")
    assert first.status_code == 200 and first.headers["content-type"] == "audio/wav"
    assert first.content[:4] == b"RIFF" and first.content[8:12] == b"WAVE"
    second = client.get("/api/voices/Leda/preview")
    assert second.content == first.content and calls == ["Leda"]  # served from cache the second time
    assert client.get("/api/voices/Nobody/preview").status_code == 404
