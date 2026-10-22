"""Reading a PDF in a separate process (the server's upload jobs) gives exactly what reading it in-process does."""

import multiprocessing
import threading
import time
from pathlib import Path

import pymupdf
import pytest

from tutor.ingest.isolated import PdfReaderDied, parse_pdf_isolated
from tutor.ingest.parse import parse_pdf

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("name", ["sample_toc", "sample_notoc"])
def test_same_document_as_in_process(name):
    path = FIXTURES / f"{name}.pdf"
    if not path.exists():
        pytest.skip("run tests/fixtures/make_sample_pdf.py first")
    assert parse_pdf_isolated(path, 5) == parse_pdf(path, 5)


def test_same_error_as_in_process(tmp_path):
    bad = tmp_path / "not_a_pdf.pdf"
    bad.write_text("this is not a pdf")
    with pytest.raises(pymupdf.FileDataError) as in_process:
        parse_pdf(bad)
    with pytest.raises(pymupdf.FileDataError) as isolated:
        parse_pdf_isolated(bad)
    assert str(isolated.value) == str(in_process.value)


def test_a_reader_that_dies_fails_the_job_not_the_server():
    def kill_reader():
        for _ in range(200):
            readers = [p for p in multiprocessing.active_children() if p.name == "pdf-reader"]
            if readers:
                readers[0].kill()
                return
            time.sleep(0.05)

    killer = threading.Thread(target=kill_reader)
    killer.start()
    with pytest.raises(PdfReaderDied):
        parse_pdf_isolated(FIXTURES / "sample_toc.pdf")
    killer.join()


def _hang(conn, path, fallback_every):  # module level: the spawned child must be able to import it
    time.sleep(30)


def test_a_reader_that_hangs_is_stopped_at_its_deadline(monkeypatch):
    from tutor.ingest import isolated

    monkeypatch.setattr(isolated, "_child", _hang)
    started = time.monotonic()
    with pytest.raises(PdfReaderDied, match="took longer than"):
        parse_pdf_isolated(FIXTURES / "sample_toc.pdf", timeout_s=2)
    assert time.monotonic() - started < 15
    assert not [p for p in multiprocessing.active_children() if p.name == "pdf-reader"]


def test_the_deadline_grows_with_the_page_count(tmp_path):
    from tutor.ingest.isolated import _deadline_for

    assert _deadline_for(FIXTURES / "sample_toc.pdf") == 300  # 6 pages: the 5-minute floor
    assert _deadline_for(tmp_path / "missing.pdf") == 300
