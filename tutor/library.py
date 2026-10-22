"""Ingest a PDF into its own folder under DATA_ROOT, and open an ingested document for a session.

Folder per document (docs/<doc_id>/): lancedb/ · sections.json · meta.json · progress.json

Embedding spaces. EmbeddingGemma (offline) and Gemini Embedding 2 (online) produce vectors that can't be compared
with each other, so a document keeps one vector index (LanceDB table) per space: same chunks, same order, vectors
from one model each. meta["vector_indexes"] records which spaces exist. A search embeds the question with the
current embedder and only ever searches that embedder's table. When the mode changes, a document missing the
current space gets it added from its stored chunks (add_embeddings: no re-parsing, progress and order unchanged);
until then it can still be searched by keywords. Documents indexed before spaces existed have a single "chunks"
table and their model in meta["embedding_model"]; they read as an index in that space, with nothing to migrate.
"""

import hashlib
import json
import re
import shutil
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from tutor import config, fileio
from tutor.document import Section, strip_tags
from tutor.ingest.chunk import chunk_document
from tutor.ingest.parse import parse_pdf
from tutor.lesson import Lesson
from tutor.retrieval.store import TABLE, ChunkStore

CURRENT_FILE = config.DATA_ROOT / "current.txt"


class DocumentError(RuntimeError):
    pass


class StaleIndexError(DocumentError):
    """The document was indexed with different settings; re-ingest `source_file` with force=True."""

    def __init__(self, message: str, source_file: Path):
        super().__init__(message)
        self.source_file = source_file


def doc_id_for(pdf: Path) -> str:
    return hashlib.sha256(pdf.read_bytes()).hexdigest()[:12]


def _expected_doc_meta() -> dict:
    """Settings that shape the chunks themselves: when these change the document is parsed again."""
    return {"chunker_version": config.CHUNKER_VERSION, "index_version": config.INDEX_VERSION}


def _doc_current(meta: dict) -> bool:
    return all(meta.get(k) == v for k, v in _expected_doc_meta().items())


# ---- embedding spaces

def embedding_space(embedder) -> dict:
    return {"model": embedder.model_name, "dim": embedder.dim, "prompt_version": config.EMBEDDING_PROMPT_VERSION}


def space_key(space: dict) -> str:
    return f"{space['model']}@{space['dim']}/p{space['prompt_version']}"


def _table_for(space: dict) -> str:
    name = f"{space['model']}_{space['dim']}_p{space['prompt_version']}".lower()
    return "vectors_" + re.sub(r"[^a-z0-9]+", "_", name).strip("_")


def vector_indexes(meta: dict) -> dict[str, dict]:
    """{space key: {model, dim, prompt_version, table, embedded_at}} for a document's metadata."""
    if "vector_indexes" in meta:
        return dict(meta["vector_indexes"])
    if meta.get("embedding_model"):  # indexed before spaces existed: one table, "chunks"
        space = {"model": meta["embedding_model"], "dim": meta.get("embedding_dim", config.EMBEDDING_DIM),
                 "prompt_version": meta.get("embedding_prompt_version", 1)}
        return {space_key(space): {**space, "table": TABLE, "embedded_at": meta.get("ingested_at")}}
    return {}


def has_space(meta: dict, embedder) -> bool:
    return space_key(embedding_space(embedder)) in vector_indexes(meta)


_LEGACY_KEYS = ("embedding_model", "embedding_dim", "embedding_prompt_version")


def _write_meta(doc_id: str, meta: dict) -> None:
    fileio.write_json(config.doc_dir(doc_id) / "meta.json", meta)


def _read_raw_meta(doc_id: str) -> dict | None:
    path = config.doc_dir(doc_id) / "meta.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


@dataclass
class IngestResult:
    doc_id: str
    title: str
    pages: int
    sections: int
    chunks: int
    structure_source: str
    skipped: bool = False
    reembedded: bool = False  # already indexed in another embedding space: only vectors were added


ProgressFn = Callable[[str, int, int], None]  # (stage, done, total)

_JUNK_TITLE = re.compile(r"^(microsoft (word|powerpoint)|untitled|document\d*$)|\.(docx?|pptx?|pdf|tex)$", re.I)


def pretty_title(title: str) -> str:
    """'Cognitive_Computing_Unit2_Notes' -> 'Cognitive Computing Unit2 Notes' (file names make poor titles,
    and the voice model would read underscores out)."""
    return re.sub(r"\s+", " ", re.sub(r"[_]+", " ", title or "")).strip() or title


def _choose_title(metadata_title: str, display_name: str | None, fallback: str) -> str:
    title = (metadata_title or "").strip()
    if len(title) < 3 or _JUNK_TITLE.search(title):
        return pretty_title(Path(display_name).stem if display_name else fallback)
    return title


def read_meta(doc_id: str) -> dict | None:
    path = config.doc_dir(doc_id) / "meta.json"
    if not path.exists():
        return None
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["title"] = pretty_title(meta.get("title", ""))
    return meta


def ingest(
    pdf: Path,
    embedder,
    force: bool = False,
    log=print,
    progress: ProgressFn | None = None,
    display_name: str | None = None,
    remember_current: bool = True,
    isolated_parse: bool = False,
) -> IngestResult:
    """Index a PDF. No-op if already indexed with the current settings; adds the current embedding space to a
    document indexed in another one; parses it again if the chunking settings changed.
    isolated_parse: read the PDF in a short-lived child process (tutor/ingest/isolated.py), for the long-running
    server, which would otherwise keep the memory the layout models take."""
    pdf = pdf.resolve()
    doc_id = doc_id_for(pdf)
    folder = config.doc_dir(doc_id)
    report = progress or (lambda stage, done, total: None)

    meta = read_meta(doc_id)
    if meta and not force:
        if _doc_current(meta) and vector_indexes(meta):
            reembedded = add_embeddings(doc_id, embedder, log=log, progress=progress)
            if remember_current:
                CURRENT_FILE.write_text(doc_id, encoding="utf-8")
            return IngestResult(doc_id, meta["title"], meta["pages"], meta["sections"], meta["chunks"],
                                meta["structure_source"], skipped=not reembedded, reembedded=reembedded)
        log("Index settings changed since this file was indexed; re-indexing.")

    report("parsing", 0, 1)
    log(f"Parsing {display_name or pdf.name} ...")
    from tutor.ingest.isolated import PdfReaderDied, parse_pdf_isolated

    try:
        parsed = (parse_pdf_isolated if isolated_parse else parse_pdf)(pdf, config.FALLBACK_SECTION_PAGES)
    except (DocumentError, PdfReaderDied):  # already say what happened
        raise
    except Exception as e:  # a damaged PDF fails deep inside the PDF library ("range object index out of range")
        raise DocumentError("Couldn't read this PDF. It may be damaged or saved in an unusual way: open it and save "
                            "it again (Print to PDF), then add it again.") from e
    title = _choose_title(parsed.title if parsed.title != pdf.stem else "", display_name, pdf.stem)
    log(f"  {parsed.page_count} pages, {len(parsed.sections)} sections (structure from {parsed.structure_source})")

    report("chunking", 0, 1)
    chunks = chunk_document(
        parsed.sections, embedder.count_tokens,
        config.CHUNK_TARGET_TOKENS, config.CHUNK_MAX_TOKENS, config.CHUNK_OVERLAP_TOKENS,
    )
    if not chunks:
        raise DocumentError("No extractable text found. Scanned PDFs need OCR, which isn't supported yet.")
    log(f"  {len(chunks)} chunks; embedding with {embedder.model_name} ...")
    report("embedding", 0, len(chunks))
    vectors = embedder.embed_documents(
        [(c.heading_path, c.text) for c in chunks],
        show_progress=progress is None,
        on_progress=(lambda done, total: report("embedding", done, total)) if progress else None,
    )
    report("saving", 0, 1)

    if folder.exists():
        shutil.rmtree(folder)  # orders change on re-ingest, so old progress is invalid too
    folder.mkdir(parents=True)
    space = embedding_space(embedder)
    table = _table_for(space)
    ChunkStore(folder / "lancedb", table).write(doc_id, chunks, vectors)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sections_json = [{k: v for k, v in asdict(s).items() if k != "paragraphs"} for s in parsed.sections]
    (folder / "sections.json").write_text(json.dumps(sections_json, indent=1, ensure_ascii=False), encoding="utf-8")
    meta = {
        "doc_id": doc_id,
        "title": title,
        "file_name": display_name or pdf.name,
        "source_file": str(pdf),
        "pages": parsed.page_count,
        "sections": len(parsed.sections),
        "chunks": len(chunks),
        "structure_source": parsed.structure_source,
        "ingested_at": now,
        **_expected_doc_meta(),
        "vector_indexes": {space_key(space): {**space, "table": table, "embedded_at": now}},
    }
    _write_meta(doc_id, meta)
    if remember_current:
        CURRENT_FILE.write_text(doc_id, encoding="utf-8")
    report("done", 1, 1)
    return IngestResult(doc_id, title, parsed.page_count, len(parsed.sections), len(chunks),
                        parsed.structure_source)


def add_embeddings(doc_id: str, embedder, log=print, progress: ProgressFn | None = None) -> bool:
    """Give a document vectors in the embedder's space, from its stored chunks. False if it already has them."""
    report = progress or (lambda stage, done, total: None)
    meta = _read_raw_meta(doc_id)
    if meta is None:
        raise DocumentError(f"Document {doc_id} isn't ingested.")
    indexes = vector_indexes(meta)
    space = embedding_space(embedder)
    key = space_key(space)
    if key in indexes:
        return False
    if not indexes:
        raise DocumentError(f"Document {doc_id} has no stored chunks to embed.")
    folder = config.doc_dir(doc_id)
    chunks = ChunkStore(folder / "lancedb", next(iter(indexes.values()))["table"]).load_chunks()
    log(f"Adding {embedder.model_name} embeddings to {pretty_title(meta.get('title', doc_id))} ({len(chunks)} chunks) ...")
    report("embedding", 0, len(chunks))
    vectors = embedder.embed_documents(
        [(c.heading_path, c.text) for c in chunks],
        show_progress=progress is None,
        on_progress=(lambda done, total: report("embedding", done, total)) if progress else None,
    )
    report("saving", 0, 1)
    table = _table_for(space)
    ChunkStore(folder / "lancedb", table).write(doc_id, chunks, vectors)
    indexes[key] = {**space, "table": table, "embedded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    meta = {k: v for k, v in (_read_raw_meta(doc_id) or meta).items() if k not in _LEGACY_KEYS}
    meta["vector_indexes"] = indexes
    _write_meta(doc_id, meta)
    report("done", 1, 1)
    return True


def missing_embeddings(embedder) -> list[str]:
    """Documents that are indexed and current, but not yet in the embedder's space (e.g. after switching mode)."""
    docs = config.DATA_ROOT / "docs"
    out = []
    for meta_path in sorted(docs.glob("*/meta.json")) if docs.exists() else []:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if _doc_current(meta) and vector_indexes(meta) and not has_space(meta, embedder):
            out.append(meta_path.parent.name)
    return out


def delete_document(doc_id: str) -> None:
    """Remove a document's index, progress and stored upload."""
    folder = config.doc_dir(doc_id)
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    upload = config.FILES_DIR / f"{doc_id}.pdf"
    if upload.exists():
        upload.unlink()


def resolve_doc_id(ref: str | None) -> str:
    """Accept a doc id, a path to an already-ingested PDF, or None for the most recent document."""
    if ref is None:
        if not CURRENT_FILE.exists():
            raise DocumentError("No document loaded yet. Run: python ingest.py <file.pdf>")
        return CURRENT_FILE.read_text(encoding="utf-8").strip()
    if Path(ref).is_file():
        return doc_id_for(Path(ref))
    return ref


@dataclass
class OpenDocument:
    doc_id: str
    meta: dict
    store: ChunkStore | None  # vectors in the current embedder's space; None until add_embeddings has run
    lesson: Lesson
    text_store: ChunkStore  # chunk text + keyword index (any space's table: the chunks are identical)

    @property
    def needs_embedding(self) -> bool:
        return self.store is None


def open_document(ref: str | None, embedder) -> OpenDocument:
    doc_id = resolve_doc_id(ref)
    folder = config.doc_dir(doc_id)
    meta_path = folder / "meta.json"
    if not meta_path.exists():
        raise DocumentError(f"Document {doc_id} isn't ingested. Run: python ingest.py <file.pdf>")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["title"] = pretty_title(meta.get("title", ""))

    expected = _expected_doc_meta()
    stale = {k: (meta.get(k), v) for k, v in expected.items() if meta.get(k) != v}
    indexes = vector_indexes(meta)
    if stale or not indexes:
        details = ", ".join(f"{k}: index={old} now={new}" for k, (old, new) in stale.items()) or "no vector index"
        raise StaleIndexError(
            f"Index was built with different settings ({details}). Re-run ingest with --force.",
            Path(meta["source_file"]),
        )
    entry = indexes.get(space_key(embedding_space(embedder)))
    text_store = ChunkStore(folder / "lancedb", (entry or next(iter(indexes.values())))["table"])

    sections = [Section(**s) for s in json.loads((folder / "sections.json").read_text(encoding="utf-8"))]
    for s in sections:  # indexes built before tag stripping may contain e.g. "<mark>"
        s.title, s.heading_path = strip_tags(s.title), strip_tags(s.heading_path)
    lesson = Lesson(meta["title"], sections, text_store.load_chunks(), folder / "progress.json")
    return OpenDocument(doc_id, meta, text_store if entry else None, lesson, text_store)


class DocumentCache:
    """Opened documents (chunks + lesson cursor + table handle), shared by the server's sessions."""

    def __init__(self, embedder):
        self.embedder = embedder
        self._docs: dict[str, OpenDocument] = {}
        self._lock = threading.Lock()

    def get(self, doc_id: str) -> OpenDocument:
        with self._lock:
            doc = self._docs.get(doc_id)
            if doc is None:
                doc = self._docs[doc_id] = open_document(doc_id, self.embedder)
            return doc

    def drop(self, doc_id: str) -> None:
        with self._lock:
            self._docs.pop(doc_id, None)
