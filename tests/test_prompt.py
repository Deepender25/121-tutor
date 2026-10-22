"""The system prompt: every placeholder filled, the date, and sources that can't be opened."""

from datetime import datetime
from types import SimpleNamespace

from tutor.document import Chunk, Section
from tutor.lesson import Lesson
from tutor.library import DocumentError
from tutor.notebooks import Notebook
from tutor.voice.prompt import build_system_prompt


def _lesson() -> Lesson:
    sections = [Section("s000", 1, "1 Rain", "1 Rain", 1, 2, first_order=0, last_order=1)]
    chunks = [Chunk(i, "s000", "1 Rain", i + 1, i + 1, f"body {i}") for i in range(2)]
    return Lesson("Rain Guide", sections, chunks, None)


class FakeCache:
    def __init__(self):
        self.good = SimpleNamespace(meta={"title": "Rain Guide"}, lesson=_lesson())

    def get(self, doc_id: str):
        if doc_id != "good":
            raise DocumentError(f"Document {doc_id} isn't ingested.")
        return self.good


def _notebook(sources: list[str], active: str | None) -> Notebook:
    return Notebook("nb1", "Rain", "", "t", "t", sources=sources, active_source=active)


def test_a_broken_source_is_listed_without_breaking_the_prompt():
    prompt = build_system_prompt(_notebook(["good", "gone"], "good"), FakeCache())
    assert "Rain Guide" in prompt and "can't be opened right now" in prompt
    assert "isn't ingested" not in prompt  # the error text stays in the log
    assert "{" not in prompt


def test_a_broken_active_source_drops_only_the_reading_position():
    prompt = build_system_prompt(_notebook(["gone", "good"], "gone"), FakeCache())
    assert "can't be opened right now" in prompt and "Rain Guide" in prompt
    assert "# Reading position" not in prompt and "{" not in prompt


def test_empty_notebook_names_the_add_button():
    prompt = build_system_prompt(_notebook([], None), FakeCache())
    assert "Add button" in prompt and "Add source" not in prompt and "{" not in prompt


def test_prompt_has_the_date_and_every_section():
    prompt = build_system_prompt(_notebook(["good"], "good"), FakeCache(), {"teaching": "exam"})
    assert f"Today is {datetime.now():%A}, " in prompt and str(datetime.now().year) in prompt
    assert "{today}" not in prompt and "{" not in prompt
    for heading in ("# Boundaries", "# Unclear audio", "# Reading position", "# Your manner"):
        assert heading in prompt
    assert "UNMISTAKABLY" in prompt and "Teach for exams" in prompt
    assert "parts_left_in_section" in prompt  # the tool result field the teaching rule refers to
