"""Notebook storage: book-cover colours stay distinct and survive old files."""

import json

from tutor.notebooks import COVERS, NotebookStore


def test_new_notebooks_get_the_least_used_cover(tmp_path):
    store = NotebookStore(tmp_path)
    covers = [store.create(f"Notebook {i}").cover for i in range(len(COVERS) + 2)]
    assert covers[:len(COVERS)] == COVERS  # every colour once before any repeats
    assert covers[len(COVERS):] == COVERS[:2]


def test_old_notebooks_get_covers_oldest_first_and_keep_them(tmp_path):
    for i, created in enumerate(["2026-03-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"]):
        (tmp_path / f"nb{i}.json").write_text(json.dumps(
            {"id": f"nb{i}", "name": f"Old {i}", "emoji": "", "created_at": created, "updated_at": created}))
    store = NotebookStore(tmp_path)
    assert store.get("nb1").cover == COVERS[0]  # the older one first
    assert store.get("nb0").cover == COVERS[1]
    assert json.loads((tmp_path / "nb0.json").read_text())["cover"] == COVERS[1]  # saved, not recomputed


def test_cover_can_be_changed_but_only_to_a_palette_colour(tmp_path):
    store = NotebookStore(tmp_path)
    nb = store.create("Chemistry")
    assert store.update(nb.id, cover="plum").cover == "plum"
    assert store.update(nb.id, cover="#ff00ff").cover == "plum"


def test_unknown_fields_in_a_notebook_file_are_ignored(tmp_path):
    (tmp_path / "nbx.json").write_text(json.dumps({"id": "nbx", "name": "X", "emoji": "", "created_at": "t",
                                                    "updated_at": "t", "from_the_future": 1}))
    assert NotebookStore(tmp_path).get("nbx").name == "X"


def test_a_damaged_notebook_file_is_skipped_not_fatal(tmp_path, caplog):
    store = NotebookStore(tmp_path)
    good = store.create("Physics")
    store.add_source(good.id, "abc123")
    (tmp_path / "deadbeef0001.json").write_text('{"id": "deadbeef0001", "name": "Half', encoding="utf-8")
    with caplog.at_level("WARNING", logger="notebooks"):
        assert [nb.id for nb in store.all()] == [good.id]
        store.all()
    assert sum("deadbeef0001.json" in r.getMessage() for r in caplog.records) == 1  # reported once
    assert store.create("x").name == "x"
    assert store.is_used("abc123") and not store.is_used("zzz")
    assert (tmp_path / "deadbeef0001.json").exists()  # left in place to be recovered


def test_names_are_cut_to_120_characters_on_create_and_rename(tmp_path):
    store = NotebookStore(tmp_path)
    nb = store.create("x" * 5000)
    assert nb.name == "x" * 120
    assert store.update(nb.id, name="y" * 5000).name == "y" * 120
    assert store.create("   ").name == "Untitled notebook"


def test_writes_leave_no_temp_files(tmp_path):
    store = NotebookStore(tmp_path)
    nb = store.create("Chemistry")
    store.update(nb.id, name="Organic chemistry")
    store.append_history(nb.id, [{"role": "user", "text": "namaste"}])
    assert NotebookStore(tmp_path).get(nb.id).name == "Organic chemistry"
    assert [p.name for p in tmp_path.iterdir()] == [f"{nb.id}.json"]
