from tutor.document import Paragraph, Section
from tutor.ingest.chunk import chunk_document, chunk_section, split_sentences


def words(text: str) -> int:
    return len(text.split())


def section(sid, level, paras, title=None):
    return Section(sid, level, title or sid, title or sid, paras[0].page if paras else 1,
                   paras[-1].page if paras else 1, paras)


def test_split_sentences_handles_hindi_danda():
    assert split_sentences("Yeh pehla hai। Second one. Third?") == ["Yeh pehla hai।", "Second one.", "Third?"]


def test_packs_paragraphs_up_to_target_and_tracks_pages():
    paras = [Paragraph(" ".join(["w"] * 40) + ".", page=1 + i // 2) for i in range(6)]
    chunks = chunk_section(section("s1", 1, paras), 0, words, target=100, max_tokens=150, overlap=0)
    assert [c.order for c in chunks] == [0, 1, 2]
    assert all(words(c.body) <= 100 for c in chunks)
    assert (chunks[0].page_start, chunks[0].page_end) == (1, 1)
    assert (chunks[2].page_start, chunks[2].page_end) == (3, 3)


def test_overlap_is_prefix_and_body_excludes_it():
    paras = [Paragraph(f"Sentence {i} " + "x " * 30 + ".", page=1) for i in range(4)]
    chunks = chunk_section(section("s1", 1, paras), 0, words, target=70, max_tokens=100, overlap=40)
    assert chunks[0].overlap_chars == 0
    second = chunks[1]
    assert second.overlap_chars > 0
    assert second.text.endswith(second.body)
    assert second.text[: second.overlap_chars].strip() in chunks[0].body


def test_oversized_paragraph_is_split():
    big = Paragraph(" ".join(f"Sentence number {i} goes here." for i in range(100)), page=4)
    chunks = chunk_section(section("s1", 1, [big]), 0, words, target=60, max_tokens=80, overlap=0)
    assert len(chunks) > 1
    assert all(words(c.body) <= 80 for c in chunks)
    assert all(c.page_start == 4 for c in chunks)


def test_tiny_tail_merges_into_previous():
    paras = [Paragraph("a " * 90, 1), Paragraph("b " * 5, 1)]
    chunks = chunk_section(section("s1", 1, paras), 0, words, target=90, max_tokens=120, overlap=0)
    assert len(chunks) == 1


def test_section_ranges_cover_descendants():
    p = lambda n: [Paragraph("word " * n, 1)]
    sections = [
        section("s1", 1, [], "Chapter 1"),          # heading only, text lives in children
        section("s2", 2, p(50), "1.1"),
        section("s3", 2, p(50), "1.2"),
        section("s4", 1, p(50), "Chapter 2"),
    ]
    chunks = chunk_document(sections, words, target=60, max_tokens=80, overlap=0)
    assert len(chunks) == 3
    assert (sections[0].first_order, sections[0].last_order) == (0, 1)
    assert (sections[1].first_order, sections[1].last_order) == (0, 0)
    assert (sections[3].first_order, sections[3].last_order) == (2, 2)
