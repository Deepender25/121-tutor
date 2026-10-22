"""The debug console prints passages, the outline and the reading position without the removed completion fields."""

import console
from tests.test_lesson import make_lesson


def test_next_outline_and_where_print(capsys):
    lesson = make_lesson()
    console.show_passage(lesson.read("next"))
    console.show_outline(lesson.outline())
    console.show_where(lesson.position())
    out = capsys.readouterr().out
    assert "1.1 Terms" in out and "Chapter 1 Basics" in out
    assert "covered" not in out and "[x]" not in out
    assert "Position: " in out and "part 1 of 2" in out


def test_where_before_starting(capsys):
    console.show_where(make_lesson().position())
    assert "Not started yet" in capsys.readouterr().out
