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


def test_coverage_rolls_up_to_parents():
    lesson = make_lesson()
    for _ in range(2):
        lesson.read("next")
    outline = lesson.outline()
    covered = {s["id"]: s["covered"] for s in outline["sections"]}
    assert covered == {"s000": False, "s001": True, "s002": False, "s003": False}
    lesson.read("next")
    assert lesson.outline()["sections"][0]["covered"] is True
    assert lesson.outline()["document_covered"] == "60%"


def test_progress_persists(tmp_path):
    lesson = make_lesson(tmp_path)
    lesson.read("s002")
    reloaded = make_lesson(tmp_path)
    assert reloaded.progress.cursor == 2
    assert reloaded.position()["section_id"] == "s002"
    assert Progress.load(tmp_path / "progress.json").last_summary == "Last read: s002, page 3-4."


def statuses(lesson):
    return {s["id"]: s["status"] for s in lesson.outline(max_level=3)["sections"]}


def test_mark_through_completes_everything_up_to_target_and_moves_cursor(tmp_path):
    lesson = make_lesson(tmp_path)
    result = lesson.mark("1.2", through=True)  # "everything up to 1.2"
    assert result["completed"] and result["marked"] == "everything up to and including 1.2 Rules"
    assert statuses(lesson) == {"s000": "completed", "s001": "completed", "s002": "completed", "s003": "not_started"}
    assert result["document_covered"] == "60%" and result["sections_completed"] == "3 of 4"
    assert lesson.resolve("next") == 3  # "continue" picks up after what's done
    assert Lesson("Test Doc", lesson.sections, lesson.chunks, tmp_path / "progress.json").progress.done == {0, 1, 2}


def test_parent_status_rolls_up_from_children_and_partial_is_in_progress():
    lesson = make_lesson()
    lesson.mark("1.1")
    assert statuses(lesson)["s000"] == "in_progress"  # one of two subsections done
    lesson.read("s002")  # reading the other one finishes the chapter
    assert statuses(lesson)["s000"] == "completed"
    lesson.read("s003")  # half of chapter 2 read
    assert statuses(lesson)["s003"] == "in_progress"


def test_unmark_clears_read_and_marked_content():
    lesson = make_lesson()
    lesson.read("start"), lesson.read("next")  # 1.1 read
    lesson.mark("chapter 1")
    lesson.mark("1.1", completed=False)
    assert statuses(lesson) == {"s000": "in_progress", "s001": "not_started", "s002": "completed", "s003": "not_started"}
    lesson.mark("all")
    assert set(statuses(lesson).values()) == {"completed"} and lesson.percent_covered() == 100
    lesson.mark("everything", completed=False)
    assert set(statuses(lesson).values()) == {"not_started"} and lesson.percent_covered() == 0


def test_mark_resolves_numbers_titles_ids_and_points():
    lesson = make_lesson()
    assert lesson.resolve_section("point 2").section_id == "s003"  # "chapter 2 practice" by number
    assert lesson.resolve_section("s002").title == "1.2 Rules"
    assert lesson.resolve_section("practice").section_id == "s003"
    assert lesson.resolve_section("page:5").section_id == "s003"
    with pytest.raises(LessonError, match="No section matches"):
        lesson.mark("quantum gravity")


def test_old_progress_files_still_load(tmp_path):
    (tmp_path / "progress.json").write_text('{"cursor": 1, "read": [0, 1], "last_summary": "x", "updated_at": "t"}')
    lesson = make_lesson(tmp_path)
    assert lesson.progress.done == set() and statuses(lesson)["s001"] == "completed"


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
    import tutor.lesson as lesson_mod
    lesson = make_lesson(tmp_path)
    monkeypatch.setattr(lesson_mod.os, "replace", _locked_replace(2))
    lesson.read("next")
    assert Progress.load(tmp_path / "progress.json").read == {0}
    assert not list(tmp_path.glob("*.tmp"))


def test_a_locked_progress_file_never_breaks_teaching(tmp_path, monkeypatch):
    import tutor.lesson as lesson_mod
    lesson = make_lesson(tmp_path)
    monkeypatch.setattr(lesson_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(lesson_mod.os, "replace", _locked_replace(100))
    passage = lesson.read("next")  # the save fails, the read still works
    assert passage["text"]
    assert lesson.progress.read == {0}
    assert not list(tmp_path.glob("*.tmp"))
    monkeypatch.undo()
    lesson.read("next")  # the next change saves everything, including the earlier read
    assert Progress.load(tmp_path / "progress.json").read == {0, 1}
