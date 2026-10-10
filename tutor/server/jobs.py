"""Background ingestion jobs with progress, one at a time (embedding is CPU-bound)."""

import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("jobs")

STAGE_WEIGHTS = {"queued": (0, 0), "parsing": (0, 15), "chunking": (15, 20), "embedding": (20, 95),
                 "saving": (95, 99), "done": (100, 100)}


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

    def to_dict(self) -> dict:
        return asdict(self)


class JobQueue:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingest")

    def submit(self, notebook_id: str, file_name: str, work: Callable[[Job], None]) -> Job:
        job = Job(secrets.token_hex(5), notebook_id, file_name)
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
        job.stage, job.done, job.total = stage, done, total
        lo, hi = STAGE_WEIGHTS.get(stage, (0, 0))
        fraction = done / total if total else 0
        job.percent = int(lo + (hi - lo) * fraction)
        if stage != previous_stage and stage != "done":
            log.info("  %s: %s%s", job.file_name, stage, f" ({total} chunks)" if stage == "embedding" else "")
        elif stage == "embedding" and job.percent // 25 > previous_percent // 25:
            log.info("  %s: embedded %d/%d", job.file_name, done, total)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def for_notebook(self, notebook_id: str, include_finished: bool = False) -> list[Job]:
        with self._lock:
            return [j for j in self._jobs.values() if j.notebook_id == notebook_id
                    and (include_finished or j.status in ("queued", "running", "error"))]

    def dismiss(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def is_pdf(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(5) == b"%PDF-"
