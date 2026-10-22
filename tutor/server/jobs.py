"""Background jobs with progress, one at a time (embedding is CPU-bound locally, rate-limited online).

kind "ingest": a PDF being added to a notebook (uploaded, or from a link: stage "fetching" gets it first). kind "reindex": an existing document getting vectors for the current
embedding model after EMBEDDING_MODEL changed; it shows in every notebook that has the document."""

import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger("jobs")

STAGE_WEIGHTS = {"queued": (0, 0), "fetching": (0, 0), "parsing": (0, 15), "chunking": (15, 20), "embedding": (20, 95),
                 "saving": (95, 99), "done": (100, 100)}
REINDEX_WEIGHTS = {**STAGE_WEIGHTS, "embedding": (0, 95)}  # no parsing: straight to embedding


@dataclass
class Job:
    id: str
    notebook_id: str
    file_name: str
    status: str = "queued"  # queued | running | done | error
    stage: str = "queued"
    done: int = 0
    total: int = 0
    percent: int = 0
    error: str | None = None
    doc_id: str | None = None
    title: str | None = None
    kind: str = "ingest"  # ingest | reindex
    stage_since: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["stage_seconds"] = int(time.monotonic() - data.pop("stage_since"))
        return data


class JobQueue:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingest")

    def submit(self, notebook_id: str, file_name: str, work: Callable[[Job], None], kind: str = "ingest",
               doc_id: str | None = None) -> Job:
        job = Job(secrets.token_hex(5), notebook_id, file_name, kind=kind, doc_id=doc_id)
        with self._lock:
            self._jobs[job.id] = job
        log.info("Queued %s (job %s)", job.file_name, job.id)
        self._pool.submit(self._run, job, work)
        return job

    def _run(self, job: Job, work: Callable[[Job], None]) -> None:
        job.status = "running"
        started = time.perf_counter()
        log.info("Indexing %s ...", job.file_name)
        try:
            work(job)
            job.status, job.stage, job.percent = "done", "done", 100
            log.info("Indexed %s as \"%s\" in %.1fs", job.file_name, job.title, time.perf_counter() - started)
        except Exception as e:  # surfaced in the UI
            log.exception("Indexing %s failed", job.file_name)
            job.status, job.error = "error", str(e) or e.__class__.__name__

    @staticmethod
    def report(job: Job, stage: str, done: int, total: int) -> None:
        previous_stage, previous_percent = job.stage, job.percent
        if stage != previous_stage:
            job.stage_since = time.monotonic()
        job.stage, job.done, job.total = stage, done, total
        lo, hi = (REINDEX_WEIGHTS if job.kind == "reindex" else STAGE_WEIGHTS).get(stage, (0, 0))
        fraction = done / total if total else 0
        job.percent = int(lo + (hi - lo) * fraction)
        if stage != previous_stage and stage != "done":
            log.info("  %s: %s%s", job.file_name, stage, f" ({total} chunks)" if stage == "embedding" else "")
        elif stage == "embedding" and job.percent // 25 > previous_percent // 25:
            log.info("  %s: embedded %d/%d", job.file_name, done, total)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def for_notebook(self, notebook_id: str, doc_ids=(), include_finished: bool = False) -> list[Job]:
        """Its uploads, plus re-indexing of any document it contains."""
        with self._lock:
            return [j for j in self._jobs.values()
                    if (j.notebook_id == notebook_id or (j.kind == "reindex" and j.doc_id in doc_ids))
                    and (include_finished or j.status in ("queued", "running", "error"))]

    def reindexing(self, doc_id: str) -> Job | None:
        """The re-index job for a document that's queued, running or failed (not yet dismissed)."""
        with self._lock:
            return next((j for j in self._jobs.values() if j.kind == "reindex" and j.doc_id == doc_id), None)

    def dismiss(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def is_pdf(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(5) == b"%PDF-"
