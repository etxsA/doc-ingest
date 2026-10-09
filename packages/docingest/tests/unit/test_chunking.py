from itertools import pairwise

import pytest

from docingest.domain.chunking import CHUNK_CHARS, OVERLAP, Chunk, chunk_pages

DOC = "ab" * 32 + "cd" * 32  # a 64-character doc_id


def test_a_short_document_is_one_chunk_with_the_page_break_appended():
    [chunk] = chunk_pages(DOC, {1: "Hello"}, chunk_chars=CHUNK_CHARS, overlap=OVERLAP)
    assert chunk == Chunk(DOC, f"{DOC[:16]} pages 1-1", "Hello\n\n", 1, 1, start=0)
    assert not chunk.is_reference


def test_no_pages_no_chunks():
    assert chunk_pages(DOC, {}, chunk_chars=CHUNK_CHARS, overlap=OVERLAP) == []


def test_chunks_are_cut_every_chunk_chars_and_overlap():
    text = "".join(chr(97 + i % 26) for i in range(100))
    full = text + "\n\n"  # 102 characters
    chunks = chunk_pages(DOC, {1: text}, chunk_chars=40, overlap=10)
    assert [c.text for c in chunks] == [full[0:40], full[30:70], full[60:100], full[90:102]]
    assert [c.start for c in chunks] == [0, 30, 60, 90]
    assert all(a.text[-10:] == b.text[:10] for a, b in pairwise(chunks))


def test_text_that_fits_with_its_page_break_is_one_chunk():
    assert len(chunk_pages(DOC, {1: "x" * 38}, chunk_chars=40, overlap=10)) == 1  # 40 characters
    assert len(chunk_pages(DOC, {1: "x" * 39}, chunk_chars=40, overlap=10)) == 2  # 41 characters


def test_chunks_record_the_pages_they_touch():
    pages = {1: "a" * 30, 2: "b" * 30, 3: "c" * 5, 4: "d" * 70}
    chunks = chunk_pages(DOC, pages, chunk_chars=50, overlap=10)
    assert [(c.first_page, c.last_page) for c in chunks] == [(1, 2), (2, 4), (4, 4), (4, 4)]
    assert [c.name for c in chunks] == [
        f"{DOC[:16]} pages {c.first_page}-{c.last_page}" for c in chunks
    ]
    assert {c.doc_id for c in chunks} == {DOC}
    joined = "".join(text + "\n\n" for text in pages.values())
    assert all(joined[c.start : c.start + len(c.text)] == c.text for c in chunks)


def test_the_page_break_keeps_words_from_fusing_across_pages():
    [chunk] = chunk_pages(DOC, {1: "end", 2: "start"}, chunk_chars=CHUNK_CHARS, overlap=OVERLAP)
    assert chunk.text == "end\n\nstart\n\n"


def test_the_settings_have_no_defaults():
    with pytest.raises(TypeError):
        chunk_pages(DOC, {1: "x"})  # type: ignore[call-arg]


@pytest.mark.parametrize(("chunk_chars", "overlap"), [(100, 100), (100, 150), (100, -1)])
def test_an_overlap_that_would_never_advance_is_rejected(chunk_chars, overlap):
    with pytest.raises(ValueError, match="overlap"):
        chunk_pages(DOC, {1: "x" * 500}, chunk_chars=chunk_chars, overlap=overlap)
