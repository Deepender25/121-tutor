"""manage.py: finding leftovers safely and making backups (in a throwaway data folder)."""

import json
import sqlite3
import tarfile

import pytest

import manage
from tutor import auth, config


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(config, "NOTEBOOKS_DIR", tmp_path / "notebooks")
    monkeypatch.setattr(config, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "AUTH_DB", tmp_path / "auth.sqlite3")
    monkeypatch.setattr(manage, "_api_running", lambda: False)
    for d in ("notebooks", "files", "docs/used/pages", "docs/orphan", "voice-previews"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "notebooks" / "nb1.json").write_text(json.dumps({"id": "nb1", "sources": ["used"]}))
    (tmp_path / "docs" / "used" / "meta.json").write_text("{}")
    (tmp_path / "docs" / "used" / "pages" / "1@1.6.png").write_bytes(b"png" * 1000)
    (tmp_path / "docs" / "orphan" / "meta.json").write_text("{}")
    (tmp_path / "files" / "used.pdf").write_bytes(b"%PDF-used")
    (tmp_path / "files" / "orphan.pdf").write_bytes(b"%PDF-orphan")
    (tmp_path / "files" / "upload-abc.part").write_bytes(b"half")
    (tmp_path / "voice-previews" / "Kore.wav").write_bytes(b"wav")
    return tmp_path


def test_leftovers_are_only_things_no_notebook_uses(data):
    found = {p.relative_to(data).as_posix() for p, _ in manage._leftovers()}
    assert found == {"docs/orphan", "files/orphan.pdf", "files/upload-abc.part"}


def test_prune_removes_leftovers_and_keeps_everything_in_use(data):
    assert manage.data_prune(yes=True) == 0
    assert not (data / "docs" / "orphan").exists() and not (data / "files" / "orphan.pdf").exists()
    assert (data / "docs" / "used" / "meta.json").exists() and (data / "files" / "used.pdf").exists()
    assert manage._leftovers() == []


def test_prune_refuses_while_the_api_runs(data, monkeypatch):
    monkeypatch.setattr(manage, "_api_running", lambda: True)
    assert manage.data_prune(yes=True) == 1
    assert (data / "docs" / "orphan").exists()


def test_backup_has_the_data_and_a_consistent_login_database_but_no_caches(data, tmp_path_factory):
    store = auth.AuthStore(config.AUTH_DB)
    store.create_user("me@example.com", "a long enough password")
    store.create_session(store.users()[0])  # leaves rows in the WAL file, not yet in the main file
    out = tmp_path_factory.mktemp("bk") / "backup.tar.gz"
    assert manage.data_backup(str(out)) == 0
    store.close()
    with tarfile.open(out) as tar:
        names = set(tar.getnames())
        assert {"notebooks/nb1.json", "docs/used/meta.json", "files/used.pdf", "auth.sqlite3"} <= names
        assert not any("pages" in n or "voice-previews" in n or n.endswith(".part") or "sqlite3-wal" in n for n in names)
        extract = tmp_path_factory.mktemp("restore")
        tar.extract("auth.sqlite3", extract, filter="data")
    db = sqlite3.connect(extract / "auth.sqlite3")
    assert db.execute("SELECT email FROM users").fetchall() == [("me@example.com",)]
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone() == (1,)
    db.close()
