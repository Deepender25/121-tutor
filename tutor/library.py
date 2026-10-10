"""Ingest a PDF into its own folder under DATA_ROOT, and open an ingested document for a session.

Folder per document (docs/<doc_id>/): lancedb/ · sections.json · meta.json · progress.json
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

from tutor import config
from tutor.document import Section, strip_tags
from tutor.ingest.chunk import chunk_document
from tutor.ingest.parse import parse_pdf
from tutor.lesson import Lesson
from tutor.retrieval.store import ChunkStore

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


def _expected_index_meta(embedder) -> dict:
    return {
        "embedding_model": embedder.model_name,
        "embedding_dim": embedder.dim,
        "embedding_prompt_version": config.EMBEDDING_PROMPT_VERSION,
        "chunker_version": config.CHUNKER_VERSION,
        "index_version": config.INDEX_VERSION,
    }


@dataclass
class IngestResult:
    doc_id: str
    title: str
    pages: int
    sections: int
    chunks: int
    structure_source: str
    skipped: bool = False


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
) -> IngestResult:
    """Index a PDF. No-op if already indexed with the current settings (re-indexes if settings changed)."""
    pdf = pdf.resolve()
    doc_id = doc_id_for(pdf)
    folder = config.doc_dir(doc_id)
    meta_path = folder / "meta.json"
    report = progress or (lambda stage, done, total: None)

    meta = read_meta(doc_id)
    if meta and not force:
        current = all(meta.get(k) == v for k, v in _expected_index_meta(embedder).items())
        if current:
            if remember_current:
                CURRENT_FILE.write_text(doc_id, encoding="utf-8")
            return IngestResult(doc_id, meta["title"], meta["pages"], meta["sections"], meta["chunks"],
                                meta["structure_source"], skipped=True)
        log("Index settings changed since this file was indexed; re-indexing.")

    report("parsing", 0, 1)
    log(f"Parsing {display_name or pdf.name} ...")
    parsed = parse_pdf(pdf, config.FALLBACK_SECTION_PAGES)
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
    ChunkStore(folder / "lancedb").write(doc_id, chunks, vectors)

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
        "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **_expected_index_meta(embedder),
    }
    meta_path.write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    if remember_current:
        CURRENT_FILE.write_text(doc_id, encoding="utf-8")
    report("done", 1, 1)
    return IngestResult(doc_id, title, parsed.page_count, len(parsed.sections), len(chunks),
                        parsed.structure_source)


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
    store: ChunkStore
    lesson: Lesson


def open_document(ref: str | None, embedder) -> OpenDocument:
    doc_id = resolve_doc_id(ref)
    folder = config.doc_dir(doc_id)
    meta_path = folder / "meta.json"
    if not meta_path.exists():
        raise DocumentError(f"Document {doc_id} isn't ingested. Run: python ingest.py <file.pdf>")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["title"] = pretty_title(meta.get("title", ""))

    expected = _expected_index_meta(embedder)
    stale = {k: (meta.get(k), v) for k, v in expected.items() if meta.get(k) != v}
    if stale:
        details = ", ".join(f"{k}: index={old} now={new}" for k, (old, new) in stale.items())
        raise StaleIndexError(
            f"Index was built with different settings ({details}). Re-run ingest with --force.",
            Path(meta["source_file"]),
        )

    sections = [Section(**s) for s in json.loads((folder / "sections.json").read_text(encoding="utf-8"))]
    for s in sections:  # indexes built before tag stripping may contain e.g. "<mark>"
        s.title, s.heading_path = strip_tags(s.title), strip_tags(s.heading_path)
    store = ChunkStore(folder / "lancedb")
    lesson = Lesson(meta["title"], sections, store.load_chunks(), folder / "progress.json")
    return OpenDocument(doc_id, meta, store, lesson)


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
