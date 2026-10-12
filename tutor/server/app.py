"""FastAPI app: REST API for notebooks/sources/pages and the live voice WebSocket. The UI is a separate
Next.js server (frontend/, ui.py) that forwards /api here locally; when hosted, the browser calls this API
directly from the frontend's site (CORS), behind the login and origin checks in security.py."""

import asyncio
import logging
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from tutor import config, web
from tutor.library import DocumentError, delete_document, doc_id_for, ingest, read_meta
from tutor.notebooks import COVERS as NOTEBOOK_COVERS
from tutor.notebooks import ICONS as NOTEBOOK_ICONS
from tutor.server import auth_routes, pages
from tutor.server.jobs import JobQueue, is_pdf
from tutor.server.live import live_endpoint
from tutor.server.security import ConfiguredCORS, SecurityMiddleware
from tutor.server.state import AppState
from tutor.tools import NotebookTools
from tutor.voice.style import STYLES, style_options

log = logging.getLogger("server")
http_log = logging.getLogger("http")
state = AppState()
# Polled or high-volume GETs are logged at DEBUG (file only) so the console stays readable.
_QUIET_GETS = ("/api/health", "/api/status", "/api/jobs/", "/api/docs/")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if problem := config.hosting_error():
        log.error(problem)
        raise RuntimeError(problem)
    state.start()
    log.info("Server ready. Data folder: %s", config.DATA_ROOT)
    if config.PUBLIC_ORIGINS:
        log.info("Hosted: login %s; accepting browser requests from %s", "on" if config.AUTH_ENABLED else "OFF",
                 ", ".join(config.PUBLIC_ORIGINS))
    yield
    log.info("Server shutting down")
    state.jobs.shutdown()
    state.stop()


app = FastAPI(title="Voice Tutor", lifespan=lifespan)
auth_routes.bind(state)
app.include_router(auth_routes.router)
# Outermost last: CORS answers preflights and labels every response (401s included); then origin + login checks.
app.add_middleware(SecurityMiddleware, auth_store_getter=lambda: state.auth)
app.add_middleware(ConfiguredCORS, allow_credentials=True, allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                   allow_headers=["Content-Type"], max_age=600)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.perf_counter()
    path = request.url.path
    try:
        response = await call_next(request)
    except Exception:
        http_log.exception("%s %s crashed", request.method, path)
        return JSONResponse({"detail": "Internal error. See logs/errors.log."}, status_code=500)
    ms = (time.perf_counter() - started) * 1000
    status = response.status_code
    if status >= 500:
        level = logging.ERROR
    elif status >= 400:
        level = logging.WARNING
    elif request.method == "GET" and (not path.startswith("/api/") or path.startswith(_QUIET_GETS)):
        level = logging.DEBUG
    else:
        level = logging.INFO
    http_log.log(level, "%s %s -> %d (%.0f ms)", request.method, path, status, ms)
    return response


# ---- helpers -----------------------------------------------------------------------------


def _notebook_or_404(notebook_id: str):
    try:
        return state.notebooks.get(notebook_id)
    except KeyError:
        raise HTTPException(404, "Notebook not found")


def _meta_or_404(doc_id: str) -> dict:
    meta = read_meta(doc_id) if doc_id.isalnum() else None
    if meta is None:
        raise HTTPException(404, "Source not found")
    return meta


def _source_pdf(doc_id: str) -> Path:
    stored = config.FILES_DIR / f"{doc_id}.pdf"
    if stored.exists():
        return stored
    original = Path(_meta_or_404(doc_id)["source_file"])
    if original.exists():
        return original
    raise HTTPException(410, "The original PDF file is missing")


def _source_info(doc_id: str) -> dict:
    meta = read_meta(doc_id)
    if meta is None:
        return {"doc_id": doc_id, "title": "(missing source)", "missing": True}
    info = {"doc_id": doc_id, "title": meta["title"], "file_name": meta.get("file_name", ""),
            "pages": meta["pages"], "chunks": meta["chunks"], "position": {"started": False}}
    if state.cache is not None:
        try:
            doc = state.cache.get(doc_id)
            info["position"] = doc.lesson.position()
            if doc.needs_embedding:  # indexed with the other embedding model: keyword search until re-embedded
                info["keyword_only"] = True
                state.ensure_embeddings(doc_id)
        except DocumentError as e:
            info["error"] = str(e)
    return info


def _notebook_payload(nb, with_history: bool = False) -> dict:
    payload = nb.summary()
    payload["sources"] = [_source_info(d) for d in nb.sources]
    if with_history:
        payload["history"] = nb.history
        payload["jobs"] = [j.to_dict() for j in state.jobs.for_notebook(nb.id, nb.sources)]
    return payload


# ---- status & settings ---------------------------------------------------------------------


@app.get("/api/health")
def health():
    return {"ok": True, "app": "voice-tutor"}


@app.get("/api/status")
def status():
    embedder = "ready" if state.embedder is not None else ("error" if state.embedder_error else "loading")
    return {
        "embedder": embedder,
        "embedder_error": state.embedder_error,
        "embedding_mode": config.EMBEDDING_MODE,
        "auth": "on" if config.AUTH_ENABLED else "off",
        "embedding_model": config.EMBEDDING_MODEL,
        "gemini_key": state.has_gemini_key(),
        "live_model": config.LIVE_MODEL,
        "web_provider": web.provider(),
        "voices": config.VOICES,
        "style_options": style_options(),
        "log_file": str(config.LOG_DIR / "voice-tutor.log"),
        **state.settings(),
    }


class SettingsIn(BaseModel):
    voice: str | None = None
    style: dict[str, str] | None = None  # any of warmth / expression / teaching (tutor/voice/style.py)


@app.put("/api/settings")
def update_settings(body: SettingsIn):
    if body.voice is not None and body.voice not in config.VOICES:
        raise HTTPException(400, "Unknown voice")
    if body.style is not None:
        for key, value in body.style.items():
            if key not in STYLES or value not in {v[0] for v in STYLES[key][1]}:
                raise HTTPException(400, f"Unknown tutor setting: {key}={value}")
    return state.update_settings(voice=body.voice, style=body.style)


@app.get("/api/voices/{voice}/preview")
async def voice_preview(voice: str):
    if voice not in config.VOICES:
        raise HTTPException(404, "Unknown voice")
    if not state.has_gemini_key():
        raise HTTPException(503, "Voice previews need a Gemini API key in .env")
    from tutor.voice import preview

    try:
        wav = await preview.get_preview(voice)
    except Exception as e:  # quota, network: tell the UI plainly
        log.warning("Voice preview for %s failed: %s", voice, e)
        raise HTTPException(502, "Couldn't generate that preview right now. Try again in a moment.")
    return Response(wav, media_type="audio/wav", headers={"Cache-Control": "max-age=86400"})


# ---- notebooks ---------------------------------------------------------------------------


class NotebookIn(BaseModel):
    name: str = "Untitled notebook"
    icon: str | None = None


class NotebookPatch(BaseModel):
    name: str | None = None
    icon: str | None = None
    cover: str | None = None
    active_source: str | None = None


@app.get("/api/notebooks")
def list_notebooks():
    return [_notebook_payload(nb) for nb in state.notebooks.all()]


@app.post("/api/notebooks", status_code=201)
def create_notebook(body: NotebookIn):
    return _notebook_payload(state.notebooks.create(body.name, body.icon))


@app.get("/api/notebooks/{notebook_id}")
def get_notebook(notebook_id: str):
    return _notebook_payload(_notebook_or_404(notebook_id), with_history=True)


@app.patch("/api/notebooks/{notebook_id}")
def patch_notebook(notebook_id: str, body: NotebookPatch):
    nb = _notebook_or_404(notebook_id)
    if body.active_source is not None and body.active_source not in nb.sources:
        raise HTTPException(400, "That source isn't in this notebook")
    if body.icon is not None and body.icon not in NOTEBOOK_ICONS:
        raise HTTPException(400, "Unknown icon")
    if body.cover is not None and body.cover not in NOTEBOOK_COVERS:
        raise HTTPException(400, "Unknown cover colour")
    nb = state.notebooks.update(notebook_id, name=body.name, icon=body.icon, cover=body.cover,
                                active_source=body.active_source)
    return _notebook_payload(nb)


@app.delete("/api/notebooks/{notebook_id}", status_code=204)
def delete_notebook(notebook_id: str):
    _notebook_or_404(notebook_id)
    for doc_id in state.notebooks.delete(notebook_id):
        if state.cache:
            state.cache.drop(doc_id)
        delete_document(doc_id)
    return Response(status_code=204)


@app.delete("/api/notebooks/{notebook_id}/history", status_code=204)
def clear_history(notebook_id: str):
    _notebook_or_404(notebook_id)
    state.notebooks.clear_history(notebook_id)
    return Response(status_code=204)


@app.get("/api/notebooks/{notebook_id}/search")
async def search_notebook(notebook_id: str, q: str = Query(..., min_length=1, max_length=500)):
    nb = _notebook_or_404(notebook_id)
    if not nb.sources:
        return {"results": [], "refs": []}
    try:
        embedder = await asyncio.to_thread(state.require_embedder)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    tools = NotebookTools(state.cache, embedder, nb.sources, nb.active_source)
    outcome = await asyncio.to_thread(tools.search_sources, q, None, 8)
    return {"refs": (outcome.ui or {}).get("refs", [])}


# ---- sources -----------------------------------------------------------------------------


@app.post("/api/notebooks/{notebook_id}/sources", status_code=202)
async def upload_sources(notebook_id: str, files: list[UploadFile]):
    _notebook_or_404(notebook_id)
    if not files:
        raise HTTPException(400, "No files")
    jobs = []
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    for upload in files:
        name = Path(upload.filename or "document.pdf").name
        part = config.FILES_DIR / f"upload-{secrets.token_hex(6)}.part"
        size = 0
        with part.open("wb") as out:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    break
                out.write(chunk)
        if size > limit:
            part.unlink(missing_ok=True)
            raise HTTPException(413, f"{name} is larger than {config.MAX_UPLOAD_MB} MB")
        if size == 0 or not is_pdf(part):
            part.unlink(missing_ok=True)
            raise HTTPException(415, f"{name} isn't a PDF")
        doc_id = await asyncio.to_thread(doc_id_for, part)
        stored = config.FILES_DIR / f"{doc_id}.pdf"
        if stored.exists():
            part.unlink()
        else:
            shutil.move(str(part), stored)
        jobs.append(state.jobs.submit(notebook_id, name, _ingest_work(notebook_id, stored, name)).to_dict())
    return {"jobs": jobs}


def _ingest_work(notebook_id: str, pdf: Path, display_name: str):
    def work(job) -> None:
        embedder = state.require_embedder(timeout=600)
        result = ingest(pdf, embedder, progress=lambda s, d, t: JobQueue.report(job, s, d, t),
                        display_name=display_name, remember_current=False, log=log.debug, isolated_parse=True)
        job.doc_id, job.title = result.doc_id, result.title
        if state.cache:
            state.cache.drop(result.doc_id)  # re-open with the fresh index
        state.notebooks.add_source(notebook_id, result.doc_id)
    return work


@app.delete("/api/notebooks/{notebook_id}/sources/{doc_id}", status_code=204)
def remove_source(notebook_id: str, doc_id: str):
    _notebook_or_404(notebook_id)
    if state.notebooks.remove_source(notebook_id, doc_id):  # nobody else uses it: delete the index
        if state.cache:
            state.cache.drop(doc_id)
        delete_document(doc_id)
    return Response(status_code=204)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = state.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job.to_dict()


@app.delete("/api/jobs/{job_id}", status_code=204)
def dismiss_job(job_id: str):
    state.jobs.dismiss(job_id)
    return Response(status_code=204)


@app.get("/api/docs/{doc_id}/outline")
async def doc_outline(doc_id: str):
    _meta_or_404(doc_id)
    await asyncio.to_thread(state.require_embedder)
    try:
        lesson = state.cache.get(doc_id).lesson
    except DocumentError as e:
        raise HTTPException(409, str(e))
    return lesson.outline(max_level=3)


@app.get("/api/docs/{doc_id}/pages")
async def page_sizes(doc_id: str):
    sizes = await asyncio.to_thread(pages.page_sizes, _source_pdf(doc_id), config.doc_dir(doc_id) / "pages")
    return {"sizes": sizes}


@app.get("/api/docs/{doc_id}/page/{page}.png")
async def page_image(doc_id: str, page: int, scale: float = 1.6):
    pdf = _source_pdf(doc_id)
    try:
        png = await asyncio.to_thread(pages.render_page, pdf, page, scale, config.doc_dir(doc_id) / "pages")
    except IndexError:
        raise HTTPException(404, "No such page")
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/docs/{doc_id}/page/{page}/find")
async def page_find(doc_id: str, page: int, text: str = Query(..., max_length=2000)):
    rects = await asyncio.to_thread(pages.find_text, _source_pdf(doc_id), page, text)
    return {"rects": rects}


@app.get("/api/docs/{doc_id}/locate")
async def page_locate(doc_id: str, text: str = Query(..., max_length=2000), first: int = 1, last: int = 0):
    """Where a sentence of a chunk is printed (page + one rectangle per line), for follow-along highlights."""
    return await asyncio.to_thread(pages.locate_text, _source_pdf(doc_id), text, first, last or first)


@app.get("/api/docs/{doc_id}/file")
def doc_file(doc_id: str):
    meta = _meta_or_404(doc_id)
    return FileResponse(_source_pdf(doc_id), media_type="application/pdf",
                        filename=meta.get("file_name") or f"{doc_id}.pdf",
                        content_disposition_type="inline")


# ---- live voice --------------------------------------------------------------------------


@app.websocket("/api/notebooks/{notebook_id}/live")
async def live(ws: WebSocket, notebook_id: str):
    if state.live_sessions >= config.MAX_LIVE_SESSIONS:  # each one holds a Gemini Live connection (and quota)
        await ws.accept()
        await ws.send_json({"type": "error", "fatal": True, "message": f"{state.live_sessions} voice sessions are "
                            "already open. End one (or close its tab) and try again."})
        await ws.close()
        return
    state.live_sessions += 1
    try:
        await live_endpoint(ws, notebook_id, state)
    finally:
        state.live_sessions -= 1


@app.exception_handler(DocumentError)
async def document_error(_, exc: DocumentError):
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    """This is the API server; the app itself is served by the UI server (or the hosted frontend)."""
    return RedirectResponse(config.APP_URL + "/", status_code=307)
