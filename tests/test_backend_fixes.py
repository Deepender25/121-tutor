"""Launcher and worker guards: no rebuild under a running UI, no new embedding worker at shutdown."""

import threading

import pytest

import ui


@pytest.fixture
def built_ui(tmp_path, monkeypatch):
    """A UI whose packages are installed and whose build is stale (the build stamp doesn't match the sources)."""
    lock_hash = ui.file_hash(ui.FRONTEND / "package-lock.json")
    for name, value in (("NEXT_BIN", "next"), ("BUILD_ID", "BUILD_ID"), ("DEPS_STAMP", "deps"),
                        ("BUILD_STAMP", "stamp")):
        monkeypatch.setattr(ui, name, tmp_path / value)
    ui.NEXT_BIN.write_text("")
    ui.BUILD_ID.write_text("x")
    ui.DEPS_STAMP.write_text(lock_hash, encoding="utf-8")
    ui.BUILD_STAMP.write_text("old sources", encoding="utf-8")
    monkeypatch.setattr(ui, "sources_hash", lambda: "new sources")
    monkeypatch.setattr(ui, "find_node", lambda: ("node", "v22.0.0"))
    monkeypatch.setattr(ui, "npm_command", lambda node: ["npm"])

    def never(*args, **kwargs):
        raise AssertionError("must not run npm or next build here")

    monkeypatch.setattr(ui, "run_logged", never)
    return tmp_path


def test_no_rebuild_while_the_ui_window_runs(built_ui, monkeypatch, capsys):
    monkeypatch.setattr(ui, "ui_running", lambda: True)
    assert ui.build() == 1
    out = capsys.readouterr().out
    assert '"Voice Tutor - UI" window from an earlier start is still running' in out
    assert "Close that window, then run run.bat again." in out


def test_no_package_install_while_the_ui_window_runs(built_ui, monkeypatch):
    monkeypatch.setattr(ui, "ui_running", lambda: True)
    ui.DEPS_STAMP.write_text("another lock file", encoding="utf-8")
    assert ui.build() == 1


def test_up_to_date_is_fine_whether_or_not_the_ui_runs(built_ui, monkeypatch, capsys):
    ui.BUILD_STAMP.write_text("new sources", encoding="utf-8")
    for running in (True, False):
        monkeypatch.setattr(ui, "ui_running", lambda: running)
        assert ui.build() == 0
        assert "Interface is up to date" in capsys.readouterr().out


def test_closed_embedding_client_never_respawns_the_worker(monkeypatch):
    from tutor.retrieval.worker import EmbedderClient

    client = EmbedderClient.__new__(EmbedderClient)  # no real worker: only the shutdown guard is under test
    client._proc = client._conn = None
    client._closed = False
    client._lock = threading.Lock()
    client.info = {}
    started = []
    monkeypatch.setattr(client, "_start", lambda: started.append(1))
    client.close()
    with pytest.raises(RuntimeError, match="shutting down"):
        client._encode_once(["text"], 1)
    with pytest.raises(RuntimeError, match="shutting down"):
        client._encode(["text"], 1)
    assert started == []


def test_a_damaged_pdf_fails_with_a_readable_message(tmp_path):
    from tutor.library import DocumentError, ingest

    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.7\n1 0 obj << /Type /Catalog >> garbage\n%%EOF")
    for isolated in (False, True):
        with pytest.raises(DocumentError, match="Couldn't read this PDF"):
            ingest(broken, None, log=lambda *a: None, remember_current=False, isolated_parse=isolated)

