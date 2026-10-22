"""Read a PDF in a separate, short-lived process, so the memory it takes is given back when it's done.

pymupdf4llm reads a PDF with neural layout models (ONNX), and the process that ran them keeps 80-120 MB per
document afterwards. Measured on the 1 GB server, in one process: 290 MB, then 410, 488 and 579 MB after each
of three PDFs; malloc_trim got back only 62 MB. The API server lives for days, so it reads each PDF in a child
process instead (started like the embedding worker, tutor/retrieval/worker.py) and gets all of it back when the
child exits. The child runs exactly parse_pdf and sends back its result, so the document comes out the same.
If reading fails, the child's own exception is raised here (same type and message); if the child dies (out of
memory, a native crash) or hangs past its deadline, the job fails with an explanation instead of the server going
down with it or the queue waiting forever.
"""

import logging
import multiprocessing as mp
import time
import traceback
from pathlib import Path

from tutor.ingest.parse import ParsedDocument

log = logging.getLogger("pdf-reader")

# Windows exit codes (NTSTATUS) of a process that ran out of memory.
NO_MEMORY_CODES = {0xC0000017, 0xC000012D}  # STATUS_NO_MEMORY, STATUS_COMMITMENT_LIMIT


class PdfReaderDied(RuntimeError):
    """The child process reading the PDF stopped without a result."""


def _child(conn, path: str, fallback_every: int) -> None:
    try:
        from tutor.ingest.parse import parse_pdf

        result = ("ok", parse_pdf(Path(path), fallback_every))
    except BaseException as e:  # sent back and raised in the server, as if it had happened there
        result = ("error", e, traceback.format_exc())
    try:
        conn.send(result)
    except Exception:  # the exception itself can't be pickled: send its type and message instead
        e = result[1]
        conn.send(("error", RuntimeError(str(e) or type(e).__name__), result[2] if len(result) > 2 else ""))
    finally:
        conn.close()


def _died(exitcode: int | None) -> str:
    code = (exitcode or 0) & 0xFFFFFFFF
    if exitcode == -9 or code in NO_MEMORY_CODES:
        return "Reading the PDF ran out of memory. Try a smaller PDF, or close other programs and try again."
    return f"Reading the PDF stopped unexpectedly (exit code {exitcode}). Details are in the server log."


def _deadline_for(path: Path) -> float:
    """Seconds a PDF may take: 5 per page, at least 5 minutes. The server reads one PDF at a time, so a reader that
    hangs on a damaged file would otherwise hold up every later upload and re-index."""
    try:
        import pymupdf

        with pymupdf.open(str(path)) as d:
            pages = d.page_count
    except Exception:  # not readable here: the child reports the real error
        pages = 0
    return max(300, 5 * pages)


def parse_pdf_isolated(path: Path, fallback_every: int = 5, timeout_s: float | None = None) -> ParsedDocument:
    started = time.perf_counter()
    if timeout_s is None:
        timeout_s = _deadline_for(path)
    deadline = time.monotonic() + timeout_s
    ctx = mp.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child, args=(sender, str(path), fallback_every), name="pdf-reader", daemon=True)
    proc.start()
    sender.close()
    try:
        # Wait for the result, checking the child is still alive: a pipe alone doesn't always report a child that
        # died early (on Windows its end of the pipe may not have been handed over yet).
        while not receiver.poll(0.5):
            if not proc.is_alive() and not receiver.poll(0):
                raise EOFError
            if time.monotonic() > deadline:
                proc.kill()
                proc.join(timeout=5)
                log.error("Reading %s took longer than %ds; stopped it", path.name, timeout_s)
                raise PdfReaderDied(f"Reading the PDF took longer than {round(timeout_s / 60)} minutes, so it was "
                                    "stopped. The PDF may be damaged or unusually complex: try saving it again "
                                    "(Print to PDF) or splitting it.")
        result = receiver.recv()
    except (EOFError, OSError):
        proc.join(timeout=10)
        log.error("The PDF reader process for %s stopped without a result (exit code %s)", path.name, proc.exitcode)
        raise PdfReaderDied(_died(proc.exitcode)) from None
    finally:
        receiver.close()
    proc.join(timeout=30)
    if proc.is_alive():  # it sent its result; it shouldn't linger, but never leave it behind
        proc.kill()
        proc.join(timeout=5)
    if result[0] == "ok":
        log.debug("Read %s in a separate process in %.1fs", path.name, time.perf_counter() - started)
        return result[1]
    log.error("Reading %s failed in the PDF reader process:\n%s", path.name, result[2])
    raise result[1]
