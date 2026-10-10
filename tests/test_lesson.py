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
