import pytest

from tutor.document import Chunk, Section
from tutor.lesson import Lesson, LessonError, Progress


def make_lesson(tmp_path=None):
    sections = [
        Section("s000", 1, "Chapter 1 Basics", "Chapter 1 Basics", 1, 4, first_order=0, last_order=2),
        Section("s001", 2, "1.1 Terms", "Chapter 1 Basics › 1.1 Terms", 1, 2, first_order=0, last_order=1),
        Section("s002", 2, "1.2 Rules", "Chapter 1 Basics › 1.2 Rules", 3, 4, first_order=2, last_order=2),
        Section("s003", 1, "Chapter 2 Practice", "Chapter 2 Practice", 5, 9, first_order=3, last_order=4),
    ]
    spec = [("s001", 1, 1), ("s001", 2, 2), ("s002", 3, 4), ("s003", 5, 6), ("s003", 8, 9)]
    chunks = [Chunk(i, sid, sid, a, b, f"overlap. body {i}", overlap_chars=9) for i, (sid, a, b) in enumerate(spec)]
    path = tmp_path / "progress.json" if tmp_path else None
    return Lesson("Test Doc", sections, chunks, path)


def test_walks_forward_and_reports_parts():
    lesson = make_lesson()
    first = lesson.read("next")
    assert first["text"] == "body 0"
    assert first["position"] == "1.1 Terms, part 1 of 2"
    assert not first["is_section_end"]
    second = lesson.read("next")
    assert second["is_section_end"] and second["pages"] == "2"


def test_navigation_targets():
    lesson = make_lesson()
    assert lesson.resolve("start") == 0
    assert lesson.resolve("page:7") == 4  # page 7 falls in a gap -> next chunk starting after it
    assert lesson.resolve("page 3") == 2
    assert lesson.resolve("s003") == 3
    assert lesson.resolve("chapter 2") == 3
    assert lesson.resolve("rules") == 2
    assert lesson.resolve("section 1.2") == 2  # number match: titles are "1.2 Rules"
    assert lesson.resolve("1.1") == 0
    lesson.read("s003")
    assert lesson.resolve("previous") == 2
    assert lesson.resolve("current") == 3


def test_errors_are_speakable():
    lesson = make_lesson()
    with pytest.raises(LessonError, match="No section matches"):
        lesson.resolve("quantum gravity")
    with pytest.raises(LessonError, match="past the end"):
        lesson.resolve("page:99")
    lesson.read("page:9")
    with pytest.raises(LessonError, match="end of the document"):
        lesson.read("next")


def test_outline_lists_sections_and_where_we_are():
    lesson = make_lesson()
    lesson.read("s002")
    outline = lesson.outline(max_level=3)
    assert [s["id"] for s in outline["sections"]] == ["s000", "s001", "s002", "s003"]
    assert set(outline["sections"][0]) == {"id", "title", "level", "pages"}  # no completion fields
    assert outline["position"]["section_id"] == "s002" and "document_covered" not in outline


def test_progress_persists(tmp_path):
    lesson = make_lesson(tmp_path)
    lesson.read("s002")
    reloaded = make_lesson(tmp_path)
    assert reloaded.progress.cursor == 2
    assert reloaded.position()["section_id"] == "s002"
    assert Progress.load(tmp_path / "progress.json").last_summary == "Last read: s002, page 3-4."


def test_old_progress_files_still_load(tmp_path):
    """Files from when completion was tracked hold read/done lists: the position loads, the rest is dropped."""
    (tmp_path / "progress.json").write_text(
        '{"cursor": 1, "read": [0, 1], "done": [2, 3], "last_summary": "x", "updated_at": "t"}')
    lesson = make_lesson(tmp_path)
    assert lesson.progress.cursor == 1 and lesson.resolve("next") == 2
    lesson.read("next")
    saved = (tmp_path / "progress.json").read_text(encoding="utf-8")
    assert '"read"' not in saved and '"done"' not in saved


def test_a_stale_position_past_the_end_starts_over(tmp_path):
    (tmp_path / "progress.json").write_text('{"cursor": 99}')
    assert make_lesson(tmp_path).progress.cursor == -1


def _locked_replace(times):
    """os.replace that fails like Windows does while another process has the target open."""
    import os
    real = os.replace
    state = {"left": times}

    def fake(src, dst):
        if state["left"]:
            state["left"] -= 1
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        real(src, dst)
    return fake


def test_progress_save_retries_while_the_file_is_briefly_locked(tmp_path, monkeypatch):
    import tutor.fileio as lesson_mod
    lesson = make_lesson(tmp_path)
    monkeypatch.setattr(lesson_mod.os, "replace", _locked_replace(2))
    lesson.read("next")
    assert Progress.load(tmp_path / "progress.json").cursor == 0
    assert not list(tmp_path.glob("*.tmp"))


def test_a_locked_progress_file_never_breaks_teaching(tmp_path, monkeypatch):
    import tutor.fileio as lesson_mod
    lesson = make_lesson(tmp_path)
    monkeypatch.setattr(lesson_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(lesson_mod.os, "replace", _locked_replace(100))
    passage = lesson.read("next")  # the save fails, the read still works
    assert passage["text"]
    assert lesson.progress.cursor == 0 and not (tmp_path / "progress.json").exists()
    assert not list(tmp_path.glob("*.tmp"))
    monkeypatch.undo()
    lesson.read("next")  # the next change saves the position
    assert Progress.load(tmp_path / "progress.json").cursor == 1
